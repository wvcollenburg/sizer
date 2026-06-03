"""Email-domain policy for signup.

Two distinct concepts live here:

* ``SCALE_DOMAIN`` — the Scale Computing employee domain. Tenants on this domain
  are flagged ``is_scale`` (cross-tenant config retrieval by code; see auth.py).
* ``PUBLIC_EMAIL_DOMAINS`` — free/consumer mailbox providers. Signup is barred on
  these because a tenant must map to an organisation, not a shared mail host.

This is product policy (a code constant), distinct from a super admin *blocking*
a real tenant domain, which is stored on the ``tenants`` row.
"""

SCALE_DOMAIN = "scalecomputing.com"


def normalize_email(email):
    """Lowercase + strip so ``Acme.com`` and ``acme.com`` never split a tenant."""
    return (email or "").strip().lower()


def domain_of(email):
    """Return the lowercased domain part of an email, or '' if malformed."""
    email = normalize_email(email)
    return email.rsplit("@", 1)[1] if "@" in email else ""


def is_public_domain(domain):
    return (domain or "").strip().lower() in PUBLIC_EMAIL_DOMAINS


# Extensive (not exhaustive) list of free/consumer mailbox providers. Kept broad
# on purpose — a corporate signer-upper will use their company domain anyway.
PUBLIC_EMAIL_DOMAINS = frozenset({
    # Google
    "gmail.com", "googlemail.com",
    # Microsoft
    "outlook.com", "hotmail.com", "hotmail.co.uk", "live.com", "live.co.uk",
    "msn.com", "outlook.co.uk", "windowslive.com", "passport.com",
    # Yahoo / Verizon
    "yahoo.com", "yahoo.co.uk", "yahoo.co.in", "yahoo.ca", "yahoo.com.au",
    "yahoo.de", "yahoo.fr", "yahoo.es", "yahoo.it", "ymail.com", "rocketmail.com",
    "aol.com", "aim.com", "verizon.net",
    # Apple
    "icloud.com", "me.com", "mac.com",
    # Proton
    "proton.me", "protonmail.com", "protonmail.ch", "pm.me",
    # Other privacy / forwarders
    "tutanota.com", "tutanota.de", "tuta.io", "tutamail.com", "keemail.me",
    "hushmail.com", "fastmail.com", "fastmail.fm", "duck.com",
    "mailfence.com", "disroot.org", "posteo.de", "posteo.net",
    # German / European
    "gmx.com", "gmx.net", "gmx.de", "gmx.at", "gmx.ch",
    "web.de", "t-online.de", "freenet.de", "mail.de",
    "orange.fr", "wanadoo.fr", "laposte.net", "free.fr", "sfr.fr", "neuf.fr",
    "libero.it", "virgilio.it", "tiscali.it", "alice.it",
    "telefonica.net", "terra.com", "wp.pl", "o2.pl", "interia.pl", "onet.pl",
    "seznam.cz", "centrum.cz", "list.ru", "bk.ru", "inbox.ru", "internet.ru",
    # Russia / Eastern Europe
    "mail.ru", "yandex.com", "yandex.ru", "ya.ru", "rambler.ru",
    # Asia
    "qq.com", "163.com", "126.com", "sina.com", "sina.cn", "sohu.com",
    "yeah.net", "foxmail.com", "naver.com", "hanmail.net", "daum.net",
    "rediffmail.com", "zoho.com", "zohomail.com",
    # Generic / catch-all consumer
    "mail.com", "email.com", "usa.com", "consultant.com", "europe.com",
    "gmx.us", "inbox.com", "lycos.com", "excite.com", "juno.com",
    "cox.net", "comcast.net", "sbcglobal.net", "att.net", "bellsouth.net",
    "btinternet.com", "blueyonder.co.uk", "ntlworld.com", "sky.com",
    "rocketmail.com", "mailinator.com", "guerrillamail.com", "10minutemail.com",
    "trashmail.com", "yopmail.com", "temp-mail.org", "getnada.com",
})
