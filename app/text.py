"""Text normalisation shared by the index build and the query side, so both see the same tokens."""
import re

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_TASHKEEL = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_AR_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ة": "ه", "ى": "ي"})

# Saudi mobiles and numbers with any separators after the country code, unified 9200 numbers, emails.
PHONE = re.compile(r"(?<!\d)(?:(?:\+|00)?966[\s-]*5(?:[\s-]*\d){8,9}|05(?:[\s-]*\d){8,9}|9200(?:[\s-]*\d){5})(?!\d)")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")


def ascii_digits(s):
    return (s or "").translate(_DIGITS)


def redact(s):
    s = ascii_digits(s)
    return EMAIL.sub("[email removed]", PHONE.sub("[phone removed]", s))


def norm(s):
    """For the FTS index and queries: ASCII digits, no Arabic diacritics, one form of alef / ta marbuta / ya."""
    return _TASHKEEL.sub("", ascii_digits(s)).translate(_AR_FOLD)


if __name__ == "__main__":
    for x in ("+966 50 123 4567", "00966 50 123 4567", "+966-50-123-4567", "05-0123-4567", "٠٥٠١٢٣٤٥٦٧", "0501234567",
              "920012345", "call me on 966501234567 or a@b.com", "للاستفسار: 0554322675_", "05045218661"):
        assert "[phone removed]" in redact(x) or "[email removed]" in redact(x), x
    for keep in ("Land 400 sqm, street 20 m, price 1600000", "lat 26.329072436716658", "licence 7100283163"):
        assert redact(keep) == keep, keep
    assert norm("مَدْرَسَة أحمد") == "مدرسه احمد"
    print("text.py ok")
