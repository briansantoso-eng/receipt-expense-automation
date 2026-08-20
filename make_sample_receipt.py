"""
Generates a folder of realistic receipts to test the pipeline against, so you
don't have to go hunting for real ones to get started.

Run it:
    python make_sample_receipt.py

Writes receipts/*.png — five deliberately different receipts:

  1. bunnings      AUD, GST-inclusive, clean               -> should be 'ok'
  2. cafe          AUD, GST-inclusive, small amounts       -> should be 'ok'
  3. cloudbase     USD, sales tax ADDED on top             -> tests the other regime
  4. fuel          AUD, GST-inclusive, litres and $/L      -> awkward layout
  5. cafe_blurry   the cafe receipt, blurred and rotated   -> should flag fields

Numbers 1-2 and 4 are Australian, where the price already includes GST, so
tax = total/11. Number 3 is American, where tax is added to the subtotal.
Getting those two regimes confused is the single most common receipt-extraction
bug, which is why the set contains both.

Number 5 exists to prove the honesty mechanism works: Claude should populate
unreadable_fields rather than confidently inventing numbers.
"""

import os

from PIL import Image, ImageDraw, ImageFilter, ImageEnhance, ImageFont

OUT_DIR = "receipts"
WIDTH = 640
MARGIN = 45


def load_font(size: int, bold: bool = False):
    """Thermal receipts are monospaced. Fall back gracefully if the font
    isn't on this machine."""
    for name in (["consolab.ttf", "courbd.ttf"] if bold
                 else ["consola.ttf", "cour.ttf"]):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


class Receipt:
    """Tiny drawing helper so five receipts don't mean five copies of the
    same layout code."""

    def __init__(self, height: int = 900):
        self.image = Image.new("RGB", (WIDTH, height), "white")
        self.draw = ImageDraw.Draw(self.image)
        self.y = 38
        self.small = load_font(17)
        self.body = load_font(20)
        self.bold = load_font(20, bold=True)
        self.big = load_font(26, bold=True)

    def line(self, text: str = "", font=None, centre: bool = False, gap: int = 27):
        font = font or self.body
        if text:
            if centre:
                width = self.draw.textlength(text, font=font)
                self.draw.text(((WIDTH - width) / 2, self.y), text,
                               fill="black", font=font)
            else:
                self.draw.text((MARGIN, self.y), text, fill="black", font=font)
        self.y += gap

    def rule(self, char: str = "-"):
        self.line(char * 44, font=self.small, gap=22)

    def row(self, label: str, value: str, font=None, gap: int = 28):
        font = font or self.body
        self.draw.text((MARGIN, self.y), label, fill="black", font=font)
        width = self.draw.textlength(value, font=font)
        self.draw.text((WIDTH - MARGIN - width, self.y), value,
                       fill="black", font=font)
        self.y += gap

    def save(self, name: str, degrade: bool = False):
        image = self.image
        if degrade:
            # A phone photo of a crumpled receipt under bad light: slight
            # rotation, soft focus, washed-out contrast.
            image = image.rotate(-2.4, expand=True, fillcolor="white")
            image = image.filter(ImageFilter.GaussianBlur(radius=1.5))
            image = ImageEnhance.Contrast(image).enhance(0.55)
            image = ImageEnhance.Brightness(image).enhance(1.12)

        path = os.path.join(OUT_DIR, f"{name}.png")
        image.save(path)
        print(f"  {path}")
        return path


def bunnings():
    r = Receipt(920)
    r.line("BUNNINGS", font=r.big, centre=True, gap=32)
    r.line("WAREHOUSE", font=r.bold, centre=True, gap=30)
    r.line("Chatswood NSW 2067", font=r.small, centre=True, gap=22)
    r.line("Ph: (02) 9411 8000", font=r.small, centre=True, gap=22)
    r.line("ABN 26 008 672 179", font=r.small, centre=True, gap=30)
    r.rule("=")
    r.line("14/08/2026            14:32", font=r.small, gap=22)
    r.line("Reg: 4   Op: SARAH   Inv: 88214", font=r.small, gap=26)
    r.rule()
    for label, price in [("3 x TIMBER SCREW 50MM", "14.20"),
                         ("1 x DROP SHEET 3.6M", "18.50"),
                         ("2 x PAINT BRUSH 50MM", "11.60"),
                         ("1 x SANDPAPER PK 5", "3.00")]:
        r.row(label, price)
    r.y += 6
    r.rule()
    r.row("SUBTOTAL", "47.30")
    r.row("TOTAL", "AUD 47.30", font=r.bold, gap=30)
    r.line("Total includes GST of      4.30", font=r.small, gap=26)
    r.rule()
    r.line("VISA CREDIT  ****4291", font=r.small, gap=22)
    r.line("APPROVED  Auth: 004512", font=r.small, gap=30)
    r.rule("=")
    r.line("Keep receipt for warranty", font=r.small, centre=True, gap=22)
    return r


def cafe():
    r = Receipt(700)
    r.line("THE DAILY GRIND", font=r.big, centre=True, gap=34)
    r.line("142 Smith St, Fitzroy VIC", font=r.small, centre=True, gap=22)
    r.line("ABN 55 112 909 441", font=r.small, centre=True, gap=30)
    r.rule("=")
    r.line("02/08/2026   08:14   Table 6", font=r.small, gap=26)
    r.rule()
    for label, price in [("2 x FLAT WHITE", "9.00"),
                         ("1 x BANANA BREAD", "6.50")]:
        r.row(label, price)
    r.y += 6
    r.rule()
    r.row("TOTAL", "$15.50", font=r.bold, gap=30)
    r.line("GST included        1.41", font=r.small, gap=26)
    r.rule()
    r.line("EFTPOS  ****1180", font=r.small, gap=22)
    r.line("Thanks - see you tomorrow!", font=r.small, centre=True, gap=22)
    return r


def cloudbase():
    """American SaaS invoice: tax is ADDED to the subtotal, not inside it."""
    r = Receipt(760)
    r.line("CLOUDBASE INC.", font=r.big, centre=True, gap=34)
    r.line("548 Market St, San Francisco CA", font=r.small, centre=True, gap=22)
    r.line("support@cloudbase.example", font=r.small, centre=True, gap=30)
    r.rule("=")
    r.line("INVOICE  #INV-2026-40881", font=r.bold, gap=28)
    r.line("Date issued:  August 1, 2026", font=r.small, gap=22)
    r.line("Billing period: Aug 1 - Aug 31", font=r.small, gap=26)
    r.rule()
    r.row("Team plan, 7 seats", "49.00")
    r.y += 6
    r.rule()
    r.row("Subtotal", "49.00")
    r.row("Sales tax (8.875%)", "4.35")
    r.row("Total due", "USD 53.35", font=r.bold, gap=30)
    r.rule()
    r.line("Paid by Mastercard ****7702", font=r.small, gap=22)
    r.line("Thank you for your business.", font=r.small, centre=True, gap=22)
    return r


def fuel():
    r = Receipt(800)
    r.line("COLES EXPRESS", font=r.big, centre=True, gap=32)
    r.line("SHELL  -  Site 4821", font=r.bold, centre=True, gap=28)
    r.line("Hume Hwy, Liverpool NSW", font=r.small, centre=True, gap=22)
    r.line("ABN 30 004 089 936", font=r.small, centre=True, gap=30)
    r.rule("=")
    r.line("09/08/2026    17:48", font=r.small, gap=22)
    r.line("Pump 6   Trans 771204", font=r.small, gap=26)
    r.rule()
    r.line("UNLEADED 91", font=r.bold, gap=26)
    r.line("34.02 L @ $1.879/L", font=r.small, gap=28)
    r.row("FUEL TOTAL", "63.92")
    r.y += 6
    r.rule()
    r.row("AMOUNT DUE", "$63.92", font=r.bold, gap=30)
    r.line("Incl GST            5.81", font=r.small, gap=26)
    r.rule()
    r.line("FLYBUYS ****2201 - 6 pts", font=r.small, gap=22)
    r.line("DEBIT CARD  ****3390", font=r.small, gap=22)
    return r


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"Writing sample receipts to {OUT_DIR}/")

    bunnings().save("bunnings")
    cafe().save("cafe")
    cloudbase().save("cloudbase")
    fuel().save("fuel")
    cafe().save("cafe_blurry", degrade=True)

    print()
    print("The correct answers, so you can mark the pipeline's work:")
    print()
    print("  file         currency  subtotal    tax    total  tax inside total?")
    print("  " + "-" * 62)
    print("  bunnings     AUD          43.00   4.30    47.30  yes")
    print("  cafe         AUD          14.09   1.41    15.50  yes")
    print("  cloudbase    USD          49.00   4.35    53.35  NO (added on top)")
    print("  fuel         AUD          58.11   5.81    63.92  yes")
    print("  cafe_blurry  AUD          14.09   1.41    15.50  yes, if legible")
    print()
    print("Traps built in on purpose:")
    print("  * bunnings prints 'SUBTOTAL 47.30', but the ex-GST subtotal is 43.00")
    print("  * cafe and fuel never print a subtotal at all — it must be derived")
    print("  * cloudbase is the ONLY one where subtotal + tax is printed directly")
    print("  * cafe_blurry should populate unreadable_fields, not invent numbers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
