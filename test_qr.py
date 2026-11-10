"""Proves the QR generator output is a real, scannable QR code by decoding it with OpenCV."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import qr  # noqa: E402

try:
    import cv2
    import numpy as np
except ImportError:  # OpenCV is only a test-time tool
    cv2 = None


def decode_png(png_bytes):
    img = cv2.imdecode(np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_GRAYSCALE)
    text, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
    return text


@unittest.skipIf(cv2 is None, "opencv not installed")
class QR(unittest.TestCase):
    URLS = [
        "https://a.co/",
        "https://exam-materials.onrender.com/",
        "https://materials.example-school-district.org/inventory/checkout",
        "https://very-long-subdomain-name.example-school-district-name.k12.ca.us/exam-materials/checkout?source=qr-code-poster",
    ]

    def test_decodes_for_each_length(self):
        for u in self.URLS:
            if len(u) > qr.MAX_BYTES:
                with self.assertRaises(ValueError):
                    qr.make(u)
                continue
            self.assertEqual(decode_png(qr.to_png(qr.make(u))), u, u)

    def test_all_eight_masks_decode(self):
        for mask in range(8):
            u = "https://exam-materials.onrender.com/"
            self.assertEqual(decode_png(qr.to_png(qr.make(u, mask=mask))), u, f"mask {mask}")

    def test_every_version_boundary(self):
        for n in (14, 15, 26, 27, 42, 43, 62, 63, 84, 85, 106):
            u = ("https://x.co/" + "a" * 200)[:n]
            self.assertEqual(decode_png(qr.to_png(qr.make(u))), u, f"{n} chars")

    def test_svg_is_wellformed(self):
        svg = qr.to_svg(qr.make("https://a.co/")).decode()
        self.assertTrue(svg.startswith("<svg") and svg.endswith("</svg>"))

    def test_too_long_rejected(self):
        with self.assertRaises(ValueError):
            qr.make("x" * 200)


if __name__ == "__main__":
    unittest.main()
