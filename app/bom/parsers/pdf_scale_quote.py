"""Scale Computing's own quotation (Salesforce CPQ, Apache FOP PDF).

Not a vendor BOM: it quotes certified Scale appliances (HE153 chassis,
HyperCore licences, support) or a price adjustment, which the checker does
not validate — certified builds are Scale's own and out of scope (see
docs/bom-checker-plan.md). Recognising it costs nothing and saves an agent
call that could only answer "no hardware to check"; the upload is refused
with a sentence saying why.
"""
from bom.parsers.pdf_common import NotAVendorBom, doc_head

FORMAT = 'scale_quote_pdf'

MESSAGE = ("This is a Scale Computing quotation, not a hardware vendor's BOM. The BOM "
           "checker validates third-party servers (Dell, Lenovo, Supermicro, HPE) against "
           "the HCL; certified Scale Computing appliances need no check.")


def detect(doc) -> bool:
    head = doc_head(doc, 40)
    if 'quotation' not in head or 'scale computing' not in head:
        return False
    return any(l.text.upper().startswith('PART #') and 'QTY' in l.text.upper() for l in doc.lines[:120])


def parse(doc):
    raise NotAVendorBom(MESSAGE)
