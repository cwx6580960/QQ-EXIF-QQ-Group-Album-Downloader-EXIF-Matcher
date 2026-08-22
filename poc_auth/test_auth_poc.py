import unittest

from auth_poc import gtk, hash33, normalize_uin, parse_jsonp, parse_ptui_callback


class AuthPocTests(unittest.TestCase):
    def test_hash33_is_stable(self):
        self.assertEqual(hash33("abc"), 108966)
        self.assertEqual(gtk("abc"), 193485963)

    def test_normalize_uin(self):
        self.assertEqual(normalize_uin("o0012345"), "12345")

    def test_parse_ptui(self):
        values = parse_ptui_callback("ptuiCB('66','0','','0','QR not scanned','');")
        self.assertEqual(values[0], "66")

    def test_parse_jsonp(self):
        self.assertEqual(parse_jsonp('callback({"ok":true});'), {"ok": True})


if __name__ == "__main__":
    unittest.main()
