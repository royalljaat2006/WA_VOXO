from whatsapp_voice.agent.conversation import ConversationStore


class TestBasics:
    def test_new_conversation_starts_empty(self):
        assert ConversationStore().get("919000000001").messages == []

    def test_same_id_returns_the_same_thread(self):
        store = ConversationStore()
        store.get("919000000001").append("user", "hi")
        assert len(store.get("919000000001").messages) == 1

    def test_different_ids_are_isolated(self):
        store = ConversationStore()
        store.get("919000000001").append("user", "campaign count?")
        assert store.get("919000000002").messages == []

    def test_reset_clears_one_thread(self):
        store = ConversationStore()
        store.get("919000000001").append("user", "hi")
        store.reset("919000000001")
        assert store.get("919000000001").messages == []


class TestHistoryTrimming:
    def test_short_history_passes_through(self):
        store = ConversationStore(max_turns=10)
        thread = store.get("n")
        thread.append("user", "a")
        thread.append("assistant", "b")
        assert len(store.history_for_request("n")) == 2

    def test_long_history_is_trimmed_to_the_window(self):
        store = ConversationStore(max_turns=4)
        thread = store.get("n")
        for i in range(10):
            thread.append("user" if i % 2 == 0 else "assistant", f"m{i}")
        assert len(store.history_for_request("n")) <= 4

    def test_trimmed_history_starts_on_a_user_turn(self):
        """The API rejects a history that opens with an assistant message."""
        store = ConversationStore(max_turns=3)
        thread = store.get("n")
        for i in range(8):
            thread.append("user" if i % 2 == 0 else "assistant", f"m{i}")
        history = store.history_for_request("n")
        assert history[0]["role"] == "user"

    def test_trimming_keeps_the_most_recent_turns(self):
        store = ConversationStore(max_turns=2)
        thread = store.get("n")
        thread.append("user", "old")
        thread.append("assistant", "older reply")
        thread.append("user", "newest")
        history = store.history_for_request("n")
        assert history[-1]["content"] == "newest"

    def test_window_with_no_user_message_is_dropped(self):
        store = ConversationStore(max_turns=1)
        thread = store.get("n")
        thread.append("user", "a")
        thread.append("assistant", "b")
        # The 1-message window is assistant-only, which is unsendable.
        assert store.history_for_request("n") == []


class TestIdleExpiry:
    def test_idle_thread_is_reset(self, monkeypatch):
        import whatsapp_voice.agent.conversation as mod

        clock = {"t": 1000.0}
        monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])

        store = ConversationStore(idle_timeout_s=60)
        store.get("n").append("user", "aaj kitne campaign gaye?")
        assert len(store.get("n").messages) == 1

        clock["t"] += 61  # user comes back much later
        assert store.get("n").messages == [], "stale context must not attach to a new question"

    def test_active_thread_survives(self, monkeypatch):
        import whatsapp_voice.agent.conversation as mod

        clock = {"t": 1000.0}
        monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])

        store = ConversationStore(idle_timeout_s=60)
        store.get("n").append("user", "first")
        clock["t"] += 30
        assert len(store.get("n").messages) == 1


class TestCapacity:
    def test_oldest_threads_are_evicted(self):
        store = ConversationStore(capacity=3)
        for i in range(5):
            store.get(f"n{i}").append("user", "hi")
        assert len(store) == 3

    def test_recently_used_threads_are_kept(self):
        store = ConversationStore(capacity=2)
        store.get("keep").append("user", "hi")
        store.get("drop").append("user", "hi")
        store.get("keep")            # touch it, making "drop" the oldest
        store.get("new").append("user", "hi")
        assert len(store.get("keep").messages) == 1
        assert store.get("drop").messages == []
