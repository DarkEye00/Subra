import re


def normalize_phone(raw):
    """Return a Safaricom mobile number as 2547XXXXXXXX or 2541XXXXXXXX, or raise ValueError."""
    raw = (raw or "").strip()
    if not re.fullmatch(r"\+?[\d\s]+", raw):
        raise ValueError("Enter a phone number like 0712 345 678")
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("254"):
        number = digits
    elif len(digits) == 10 and digits.startswith("0"):
        number = "254" + digits[1:]
    elif len(digits) == 9:
        number = "254" + digits
    else:
        raise ValueError("Enter a phone number like 0712 345 678")
    if number[3] not in "71":
        raise ValueError("Enter a Safaricom M-Pesa number starting 07 or 01")
    return number
