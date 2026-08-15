import unittest
from pathlib import Path


class PrivateOwnerLiveAuthStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).resolve().parents[1] / "handlers.py").read_text(encoding="utf-8")

    def _section(self, start: str, end: str) -> str:
        begin = self.source.index(start)
        finish = self.source.index(end, begin)
        return self.source[begin:finish]

    def test_private_panel_filters_stored_channels_through_live_owner_gate(self):
        section = self._section('@router.message(Command("panel"))', '@router.message(Command("set_period"))')
        self.assertIn("_require_private_owner_channel", section)
        self.assertIn("action=ChannelAction.PANEL", section)

    def test_private_channel_selection_rechecks_live_owner_gate_before_persisting(self):
        section = self._section('@router.callback_query(F.data.startswith("panel:select:"))', '@router.callback_query(F.data == "panel:search")')
        gate = section.index("_require_private_owner_channel")
        persist = section.index("set_active_admin_channel")
        self.assertLess(gate, persist)
        self.assertIn("action=ChannelAction.PANEL", section)

    def test_private_search_entry_and_stale_state_paths_recheck_live_owner_gate(self):
        command = self._section('@router.message(Command("search")', '# --------------------------------------------------------------\n    # Panel callbacks')
        self.assertIn("_require_private_owner_channel", command)
        self.assertIn("action=ChannelAction.SEARCH", command)

        query = self._section('@router.message(SearchFlow.query)', 'def forum_topic_url')
        page = self._section('@router.callback_query(F.data.startswith("search:page:"))', '@router.callback_query(F.data.startswith("search:open:"))')
        opened = self._section('@router.callback_query(F.data.startswith("search:open:"))', '@router.callback_query(F.data.startswith("panel:"))')
        for section in (query, page, opened):
            self.assertIn("_require_private_owner_channel", section)
            self.assertIn("action=ChannelAction.SEARCH", section)


if __name__ == "__main__":
    unittest.main()
