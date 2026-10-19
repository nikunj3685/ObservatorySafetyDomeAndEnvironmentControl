import os, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()
DEFAULT_PW = os.environ.get("RA_DEFAULT_PW", "")  # supplied by the runner, never hard-coded
sent = []
mod._ra_send_email = lambda subject, body, cfg=None: (sent.append((subject, body)) or None) if mod._ra_email_configured(cfg) else "not configured"

REMOTE = {"REMOTE_ADDR": "100.101.22.7"}
LAN = {"REMOTE_ADDR": "192.168.1.42"}
def client(env):
    c = mod.app.test_client(); c.environ_base.update(env); return c
H = {"Accept": "text/html"}

# ---- address classification
for ip, local in [("127.0.0.1", True), ("::1", True), ("192.168.1.5", True), ("10.0.0.2", True), ("172.20.1.1", True),
                  ("fe80::1", True), ("::ffff:192.168.1.9", True), ("100.64.0.1", False), ("100.127.255.254", False),
                  ("fd7a:115c:a1e0::1", False), ("8.8.8.8", False), ("garbage", False), ("", False)]:
    ck(f"ip {ip!r} local={local}", mod._ra_ip_is_local(ip) == local)

# ---- local: no login, Remote Access visible
lan = client(LAN)
ck("lan: dashboard ok", lan.get("/", headers=H).status_code == 200)
pg = lan.get("/settings").get_data(as_text=True)
ck("lan: Remote Access in menu", 'data-g="remote-access"' in pg and 'id="remote-access"' in pg)
ck("lan: no hash or smtp pw in page", "scrypt:" not in pg)
ck("lan: no remote badge", "nav-remote\"" not in pg.split("<nav class=\"topnav\">")[1].split("</nav>")[0])
ck("lan: /login redirects home", lan.get("/login").headers["Location"] == "/")

# ---- remote: gated
rem = client(REMOTE)
r = rem.get("/", headers=H)
ck("remote: page -> login redirect", r.status_code == 302 and r.headers["Location"].startswith("/login?next=%2F"))
ck("remote: fetch -> 401", rem.get("/fragments").status_code == 401)
ck("remote: open dome refused", rem.get("/open").status_code == 401)
ck("remote: restart refused", rem.get("/restart-service").status_code == 401)
ck("remote: alpaca refused", rem.get("/api/v1/dome/0/connected").status_code == 403)
ck("remote: management refused", rem.get("/management/apiversions").status_code == 403)
ck("remote: login page renders", "Remote access" in rem.get("/login").get_data(as_text=True))

# email not configured -> password ok but cannot proceed
r = rem.post("/login", data={"username": "admin", "password": DEFAULT_PW, "next": "/settings"})
ck("remote: no email configured message", "isn't set up yet" in r.get_data(as_text=True).replace("&#x27;", "'"))

# configure email from LAN (POST), with mismatched pw first
r = lan.post("/remote-access-save", data={"username": "admin", "pw1": "abcdefghijk", "pw2": "zzz", "email_to": "me@example.com",
             "smtp_host": "smtp.example.com", "smtp_port": "587", "smtp_user": "me@example.com", "smtp_password": "app-pw", "trust_days": "7", "enabled": "on"})
ck("lan save: mismatch rejected", "ra_ok=0" in r.headers["Location"] and mod.get_setting("remote_access")["email_to"] == "")
r = lan.post("/remote-access-save", data={"username": "admin", "email_to": "me@example.com", "smtp_host": "smtp.example.com",
             "smtp_port": "587", "smtp_user": "me@example.com", "smtp_password": "app-pw", "trust_days": "7", "enabled": "on"})
cfg = mod.get_setting("remote_access")
ck("lan save: email saved, pw kept", "ra_ok=1" in r.headers["Location"] and cfg["email_to"] == "me@example.com" and mod.check_password_hash(cfg["password_hash"], DEFAULT_PW))
ck("lan save: GET not allowed", lan.get("/remote-access-save").status_code == 405)
pg = lan.get("/settings").get_data(as_text=True)
ck("lan: smtp password not echoed", "app-pw" not in pg and "saved — leave blank to keep" in pg)

# wrong password
r = rem.post("/login", data={"username": "admin", "password": "nope"})
ck("remote: wrong password", "Wrong username or password" in r.get_data(as_text=True))
ck("remote: no email on wrong pw", not sent)
# right password -> code emailed
r = rem.post("/login", data={"username": "admin", "password": DEFAULT_PW, "next": "/settings"})
ck("remote: right password -> code page", r.status_code == 302 and r.headers["Location"] == "/login/code" and len(sent) == 1)
code = re.search(r"\b(\d{6})\b", sent[-1][0]).group(1)
ck("remote: email names requester ip", "100.101.22.7" in sent[-1][1])
ck("remote: still gated before code", rem.get("/", headers=H).status_code == 302)
bad = "000000" if code != "000000" else "111111"
r = rem.post("/login/code", data={"code": bad})
ck("remote: wrong code tries left", "2 tries left" in r.get_data(as_text=True))
r = rem.post("/login/resend")
ck("remote: resend throttled", "wait" in r.headers["Location"])
r = rem.post("/login/code", data={"code": code, "trust": "on"})
ck("remote: right code -> next", r.status_code == 302 and r.headers["Location"] == "/settings")
ck("remote: dashboard now ok", rem.get("/", headers=H).status_code == 200)
ck("remote: fragments ok", rem.get("/fragments").status_code == 200)
pg = rem.get("/settings").get_data(as_text=True)
ck("remote: Remote Access hidden", "remote-access" not in pg)
ck("remote: badge + sign out", "🌐 Remote · admin" in pg and 'href="/logout"' in pg)
ck("remote: settings save refused even signed in", rem.post("/remote-access-save", data={"username": "x"}).status_code == 403
   and mod.get_setting("remote_access")["username"] == "admin")
ck("remote: test email refused", rem.post("/remote-access-test-email").status_code == 403)
ck("remote: signout-all refused", rem.post("/remote-access-signout-all").status_code == 403)
ck("remote: alpaca still refused signed in", rem.get("/api/v1/dome/0/connected").status_code == 403)
ck("remote: open redirect blocked", mod._ra_safe_next("//evil.com") == "/" and mod._ra_safe_next("https://x") == "/")

# sign out all from LAN kills remote session
ck("lan: signout-all ok", lan.post("/remote-access-signout-all").get_json()["ok"])
ck("remote: session revoked", rem.get("/", headers=H).status_code == 302)

# three wrong codes -> back to password
rem2 = client({"REMOTE_ADDR": "100.90.1.1"})
rem2.post("/login", data={"username": "admin", "password": DEFAULT_PW})
for i in range(3):
    r = rem2.post("/login/code", data={"code": bad if i < 2 else ("222222" if code != "222222" else "333333")})
ck("remote: 3 wrong codes -> restart", r.status_code == 302 and r.headers["Location"].startswith("/login?msg="))

# lockout after 5 failures
rem3 = client({"REMOTE_ADDR": "100.80.1.1"})
for _ in range(5):
    rem3.post("/login", data={"username": "admin", "password": "bad"})
n = len(sent)
r = rem3.post("/login", data={"username": "admin", "password": DEFAULT_PW})
ck("remote: locked out after 5", "Too many wrong attempts" in r.get_data(as_text=True) and len(sent) == n)

# password change from LAN
r = lan.post("/remote-access-save", data={"username": "admin", "pw1": "NewPassword123", "pw2": "NewPassword123", "email_to": "me@example.com",
             "smtp_host": "smtp.example.com", "smtp_port": "587", "smtp_user": "me@example.com", "trust_days": "7", "enabled": "on"})
cfg = mod.get_setting("remote_access")
ck("lan: password changed", mod.check_password_hash(cfg["password_hash"], "NewPassword123") and cfg["smtp_password"] == "app-pw")
ck("lan: short password rejected", "ra_ok=0" in lan.post("/remote-access-save", data={"username": "admin", "pw1": "short", "pw2": "short"}).headers["Location"])

# disabled -> outside gets in without login (explicit choice), but settings still refused
lan.post("/remote-access-save", data={"username": "admin", "email_to": "me@example.com", "smtp_host": "smtp.example.com",
         "smtp_port": "587", "smtp_user": "me@example.com", "trust_days": "7"})
ck("disabled: outside open", client({"REMOTE_ADDR": "100.70.1.1"}).get("/", headers=H).status_code == 200)
ck("disabled: settings save still refused", client({"REMOTE_ADDR": "100.70.1.1"}).post("/remote-access-save").status_code == 403)
ck("secret key persisted", len(mod.get_setting("remote_access")["secret_key"]) == 64)
ck("Security log category", "Security" in mod.LOG_CATEGORIES)
ck.done()
