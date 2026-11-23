"""
Unit tests for code_fetcher regex extraction across 5 realistic ATS and email formats.
"""
import unittest
from app.services.code_fetcher import extract_code_from_email_body


class TestVerificationCodeExtractor(unittest.TestCase):

    def test_greenhouse_numeric_code(self):
        body = """
        Hi Siddhesh,
        
        Your verification code for your job application at Stripe is: 849201
        
        This code is valid for 15 minutes. Please do not share this code.
        """
        code = extract_code_from_email_body(body)
        self.assertEqual(code, "849201")

    def test_lever_confirmation_code(self):
        body = """
        Thank you for applying to Figma.
        Please use the following one-time code to confirm your email: 619420.
        If you did not request this, please ignore this email.
        """
        code = extract_code_from_email_body(body)
        self.assertEqual(code, "619420")

    def test_hyphenated_passcode(self):
        body = """
        Ashby Application Security
        
        Your security passcode is: 392-581
        
        Enter this code in your browser window to continue.
        """
        code = extract_code_from_email_body(body)
        self.assertEqual(code, "392581")

    def test_standalone_pin_on_newline(self):
        body = """
        Workday Account Verification
        
        Enter the following verification PIN:
        
        729481
        
        Thank you for your interest.
        """
        code = extract_code_from_email_body(body)
        self.assertEqual(code, "729481")

    def test_alphanumeric_access_code(self):
        body = """
        Datadog Career Portal
        
        Use access code: K9B4X2 to verify your identity and submit your profile.
        """
        code = extract_code_from_email_body(body)
        self.assertEqual(code, "K9B4X2")

    def test_imap_polling_mock(self):
        from unittest.mock import patch, MagicMock
        from app.services.code_fetcher import _poll_code_via_imap

        mock_mail = MagicMock()
        mock_mail.search.return_value = ("OK", [b"1 2"])
        
        sample_email = (
            b"From: jobs@lever.co\r\n"
            b"Subject: Your Verification Code\r\n"
            b"Content-Type: text/plain\r\n\r\n"
            b"Your verification code is: 582910\r\n"
        )
        mock_mail.fetch.return_value = ("OK", [(b"1 (RFC822 {100})", sample_email)])

        with patch("imaplib.IMAP4_SSL", return_value=mock_mail):
            code = _poll_code_via_imap(
                user="test@example.com",
                password="testpassword",
                sender_hint="lever",
                timeout=2,
                poll_interval=1
            )
            self.assertEqual(code, "582910")


if __name__ == "__main__":
    unittest.main()
