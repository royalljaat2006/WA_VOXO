from datetime import date

from whatsapp_voice.agent.claude_agent import SYSTEM_PROMPT, system_prompt_for
from whatsapp_voice.agent.formatting import for_whatsapp


class TestForWhatsApp:
    def test_double_asterisk_becomes_single(self):
        """WhatsApp bold is *one* asterisk; two arrive as literal punctuation."""
        assert for_whatsapp("Aaj **18 campaigns** gaye") == "Aaj *18 campaigns* gaye"

    def test_multiple_bolds_in_one_line(self):
        assert for_whatsapp("**a** and **b**") == "*a* and *b*"

    def test_bold_spanning_lines(self):
        assert for_whatsapp("**line one\nline two**") == "*line one\nline two*"

    def test_headings_are_removed(self):
        assert for_whatsapp("## Delivery report\n2841 sent") == "Delivery report\n2841 sent"

    def test_all_heading_levels(self):
        assert for_whatsapp("###### deep") == "deep"

    def test_markdown_bullets_become_middots(self):
        assert for_whatsapp("- sent\n- delivered") == "• sent\n• delivered"

    def test_asterisk_and_plus_bullets_too(self):
        assert for_whatsapp("* one\n+ two") == "• one\n• two"

    def test_indented_bullets_keep_their_indent(self):
        assert for_whatsapp("  - nested") == "• nested"  # leading strip is fine

    def test_bold_inside_a_bullet_survives_both_rules(self):
        """Order matters: collapsing ** first stops its * being read as a bullet."""
        assert for_whatsapp("- **Sent:** 2,841") == "• *Sent:* 2,841"

    def test_extra_blank_lines_collapse(self):
        assert for_whatsapp("a\n\n\n\nb") == "a\n\nb"

    def test_plain_prose_is_untouched(self):
        text = "2,841 bheje, 2,824 deliver hue — 99.4% delivery rate."
        assert for_whatsapp(text) == text

    def test_single_asterisk_bold_is_preserved(self):
        assert for_whatsapp("Aaj *18* gaye") == "Aaj *18* gaye"

    def test_empty_and_whitespace(self):
        assert for_whatsapp("") == ""
        assert for_whatsapp("   \n  ") == ""

    def test_lone_asterisks_are_left_alone(self):
        assert for_whatsapp("****") == "****"

    def test_multiplication_is_not_mangled(self):
        assert for_whatsapp("2 * 3 = 6") == "2 * 3 = 6"


class TestSystemPromptFor:
    def test_appends_todays_date(self):
        prompt = system_prompt_for(today=date(2026, 7, 28))
        assert "Today's date is 2026-07-28." in prompt

    def test_keeps_the_base_instructions(self):
        prompt = system_prompt_for(today=date(2026, 7, 28))
        assert SYSTEM_PROMPT in prompt

    def test_defaults_to_actual_today(self):
        assert date.today().isoformat() in system_prompt_for()

    def test_accepts_a_custom_base(self):
        assert system_prompt_for(today=date(2026, 1, 1), base="Be brief.").startswith(
            "Be brief."
        )

    def test_the_date_is_why_this_exists(self):
        """Regression: without a date anchor a model guesses from training data,
        the guess falls outside the demo range, and the tool budget burns out."""
        assert "date is" in system_prompt_for(today=date(2026, 7, 28)).lower()
