    def run_conversation(
        self,
        user_message: str,
        system_message: str = None,
        conversation_history: List[Dict[str, Any]] = None,
        task_id: str = None,
        stream_callback: Optional[callable] = None,
        persist_user_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Run a complete conversation with tool calling until completion.

        Args:
            user_message (str): The user's message/question
            system_message (str): Custom system message (optional, overrides ephemeral_system_prompt if provided)
            conversation_history (List[Dict]): Previous conversation messages (optional)
            task_id (str): Unique identifier for this task to isolate VMs between concurrent tasks (optional, auto-generated if not provided)
            stream_callback: Optional callback invoked with each text delta during streaming.
                Used by the TTS pipeline to start audio generation before the full response.
                When None (default), API calls use the standard non-streaming path.
            persist_user_message: Optional clean user message to store in
                transcripts/history when user_message contains API-only
                synthetic prefixes.
                    or queuing follow-up prefetch work.

        Returns:
            Dict: Complete conversation result with final response and message history
        """
        # Guard stdio against OSError from broken pipes (systemd/headless/daemon).
        # Installed once, transparent when streams are healthy, prevents crash on write.
        _install_safe_stdio()

        self._ensure_db_session()

        # Tell auxiliary_client what the live main provider/model are for
        # this turn. Used by tools whose behaviour depends on the active
        # main model (e.g. vision_analyze's native fast path) so they see
        # the CLI/gateway override instead of the stale config.yaml
        # default. Idempotent — fine to call every turn.
        try:
            from agent.auxiliary_client import set_runtime_main
            set_runtime_main(
                getattr(self, "provider", "") or "",
                getattr(self, "model", "") or "",
            )
        except Exception:
            pass

        # Tag all log records on this thread with the session ID so
        # ``hermes logs --session <id>`` can filter a single conversation.
        from hermes_logging import set_session_context
        set_session_context(self.session_id)

        # Bind the skill write-origin ContextVar for this thread so tool
        # handlers (e.g. skill_manage create) can tell whether they are
        # running inside the background self-improvement review fork vs.
        # a foreground user-directed turn. Set at the top of each call;
        # the review fork runs on its own thread with a fresh context,
        # so the foreground value here does not leak into it.
        from tools.skill_provenance import set_current_write_origin
        set_current_write_origin(getattr(self, "_memory_write_origin", "assistant_tool"))

        # If the previous turn activated fallback, restore the primary
        # runtime so this turn gets a fresh attempt with the preferred model.
        # No-op when _fallback_activated is False (gateway, first turn, etc.).
        self._restore_primary_runtime()

        # Sanitize surrogate characters from user input.  Clipboard paste from
        # rich-text editors (Google Docs, Word, etc.) can inject lone surrogates
        # that are invalid UTF-8 and crash JSON serialization in the OpenAI SDK.
        if isinstance(user_message, str):
            user_message = _sanitize_surrogates(user_message)
        if isinstance(persist_user_message, str):
            persist_user_message = _sanitize_surrogates(persist_user_message)

        # Store stream callback for _interruptible_api_call to pick up
        self._stream_callback = stream_callback
        self._persist_user_message_idx = None
        self._persist_user_message_override = persist_user_message
        # Generate unique task_id if not provided to isolate VMs between concurrent tasks
        effective_task_id = task_id or str(uuid.uuid4())
        # Expose the active task_id so tools running mid-turn (e.g. delegate_task
        # in delegate_tool.py) can identify this agent for the cross-agent file
        # state registry.  Set BEFORE any tool dispatch so snapshots taken at
        # child-launch time see the parent's real id, not None.
        self._current_task_id = effective_task_id
        
        # Reset retry counters and iteration budget at the start of each turn
        # so subagent usage from a previous turn doesn't eat into the next one.
        self._invalid_tool_retries = 0
        self._invalid_json_retries = 0
        self._empty_content_retries = 0
        self._incomplete_scratchpad_retries = 0
        self._codex_incomplete_retries = 0
        self._thinking_prefill_retries = 0
        self._post_tool_empty_retried = False
        self._last_content_with_tools = None
        self._last_content_tools_all_housekeeping = False
        self._mute_post_response = False
        self._unicode_sanitization_passes = 0
        self._tool_guardrails.reset_for_turn()
        self._tool_guardrail_halt_decision = None
        # True until the server rejects an image_url content part with an error
        # like "Only 'text' content type is supported."  Set to False on first
        # rejection and kept False for the rest of the session so we never re-send
        # images to a text-only endpoint.  Scoped per `_run()` call, not per instance.
        self._vision_supported = True

        # Pre-turn connection health check: detect and clean up dead TCP
        # connections left over from provider outages or dropped streams.
        # This prevents the next API call from hanging on a zombie socket.
        if self.api_mode != "anthropic_messages":
            try:
                if self._cleanup_dead_connections():
                    self._emit_status(
                        "🔌 Detected stale connections from a previous provider "
                        "issue — cleaned up automatically. Proceeding with fresh "
                        "connection."
                    )
            except Exception:
                pass
        # Replay compression warning through status_callback for gateway
        # platforms (the callback was not wired during __init__).
        if self._compression_warning:
            self._replay_compression_warning()
            self._compression_warning = None  # send once

        # NOTE: _turns_since_memory and _iters_since_skill are NOT reset here.
        # They are initialized in __init__ and must persist across run_conversation
        # calls so that nudge logic accumulates correctly in CLI mode.
        self.iteration_budget = IterationBudget(self.max_iterations)

        # Log conversation turn start for debugging/observability
        _preview_text = _summarize_user_message_for_log(user_message)
        _msg_preview = (_preview_text[:80] + "...") if len(_preview_text) > 80 else _preview_text
        _msg_preview = _msg_preview.replace("\n", " ")
        logger.info(
            "conversation turn: session=%s model=%s provider=%s platform=%s history=%d msg=%r",
            self.session_id or "none", self.model, self.provider or "unknown",
            self.platform or "unknown", len(conversation_history or []),
            _msg_preview,
        )

        # Initialize conversation (copy to avoid mutating the caller's list)
        messages = list(conversation_history) if conversation_history else []

        # Hydrate todo store from conversation history (gateway creates a fresh
        # AIAgent per message, so the in-memory store is empty -- we need to
        # recover the todo state from the most recent todo tool response in history)
        if conversation_history and not self._todo_store.has_items():
            self._hydrate_todo_store(conversation_history)

        # Hydrate per-session nudge counters from persisted history.
        # Gateway creates a fresh AIAgent per inbound message (cache miss /
        # 1h idle eviction / config-signature mismatch / process restart), so
        # _turns_since_memory and _user_turn_count start at 0 every turn and
        # the memory.nudge_interval trigger may never be reached. Reconstruct
        # an effective count from prior user turns in conversation_history.
        # Idempotent: a cached agent that already accumulated counters keeps
        # them; only a freshly-built agent with empty in-memory state hydrates.
        # See issue #22357.
        if conversation_history and self._user_turn_count == 0:
            prior_user_turns = sum(
                1 for m in conversation_history if m.get("role") == "user"
            )
            if prior_user_turns > 0:
                self._user_turn_count = prior_user_turns
                if self._memory_nudge_interval > 0 and self._turns_since_memory == 0:
                    # % preserves original 1-in-N cadence rather than firing a
                    # review immediately on resume (which would surprise users
                    # whose session happened to land just past a multiple of N).
                    self._turns_since_memory = prior_user_turns % self._memory_nudge_interval


        # Prefill messages (few-shot priming) are injected at API-call time only,
        # never stored in the messages list. This keeps them ephemeral: they won't
        # be saved to session DB, session logs, or batch trajectories, but they're
        # automatically re-applied on every API call (including session continuations).
        
        # Track user turns for memory flush and periodic nudge logic
        self._user_turn_count += 1

        # Reset the streaming context scrubber at the top of each turn so a
        # hung span from a prior interrupted stream can't taint this turn's
        # output.
        scrubber = getattr(self, "_stream_context_scrubber", None)
        if scrubber is not None:
            scrubber.reset()
        # Reset the think scrubber for the same reason — an interrupted
        # prior stream may have left us inside an unterminated block.
        think_scrubber = getattr(self, "_stream_think_scrubber", None)
        if think_scrubber is not None:
            think_scrubber.reset()

        # Preserve the original user message (no nudge injection).
        original_user_message = persist_user_message if persist_user_message is not None else user_message

        # Track memory nudge trigger (turn-based, checked here).
        # Skill trigger is checked AFTER the agent loop completes, based on
        # how many tool iterations THIS turn used.
        _should_review_memory = False
        if (self._memory_nudge_interval > 0
                and "memory" in self.valid_tool_names
                and self._memory_store):
            self._turns_since_memory += 1
            if self._turns_since_memory >= self._memory_nudge_interval:
                _should_review_memory = True
                self._turns_since_memory = 0

        # Add user message
        user_msg = {"role": "user", "content": user_message}
        messages.append(user_msg)
        current_turn_user_idx = len(messages) - 1
        self._persist_user_message_idx = current_turn_user_idx
        
        if not self.quiet_mode:
            _print_preview = _summarize_user_message_for_log(user_message)
            self._safe_print(f"💬 Starting conversation: '{_print_preview[:60]}{'...' if len(_print_preview) > 60 else ''}'")
        
        # ── System prompt (cached per session for prefix caching) ──
        # Built once on first call, reused for all subsequent calls.
        # Only rebuilt after context compression events (which invalidate
        # the cache and reload memory from disk).
        #
        # For continuing sessions (gateway creates a fresh AIAgent per
        # message), we load the stored system prompt from the session DB
        # instead of rebuilding.  Rebuilding would pick up memory changes
        # from disk that the model already knows about (it wrote them!),
        # producing a different system prompt and breaking the Anthropic
        # prefix cache.
        if self._cached_system_prompt is None:
            stored_prompt = None
