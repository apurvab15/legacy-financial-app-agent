"""Local legacy-core mock (Phase 1). Server-rendered HTML, no SPA, no Playwright."""

from __future__ import annotations

import re
import time

from flask import Flask, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.secret_key = "legacy-core-mock-local-only"

MEMBERS = {
    "12345": {
        "name": "Jane Member",
        "savings_balance": "1,240.55",
        "masked_id": "***-**-0000",
    }
}

DIGIT_RE = re.compile(r"^\d+$")
SLOW_SECONDS = 4


def _member_id_from_form() -> str:
    raw = request.form.get("ctl00$Main$txtCIF") or request.form.get("ctl00_Main_txtCIF") or ""
    return raw.strip()


def _want_slow() -> bool:
    return request.args.get("slow") == "1" or request.form.get("slow") == "1"


def _inject_expired() -> None:
    if request.args.get("expired") == "1" or request.form.get("expired") == "1":
        session["expired"] = True


@app.get("/")
def shell():
    qs = []
    if request.args.get("expired") == "1":
        qs.append("expired=1")
    if request.args.get("slow") == "1":
        qs.append("slow=1")
    content_src = "/inquiry"
    if qs:
        content_src = f"/inquiry?{'&'.join(qs)}"
    return render_template("shell.html", content_src=content_src)


@app.get("/nav")
def nav():
    return render_template("nav.html")


@app.get("/inquiry")
def inquiry_get():
    _inject_expired()
    if session.get("expired"):
        return render_template("expired.html")
    return render_template(
        "lookup.html",
        error=None,
        slow=request.args.get("slow") == "1",
    )


@app.post("/inquiry")
def inquiry_post():
    _inject_expired()
    if _want_slow():
        time.sleep(SLOW_SECONDS)
    if session.get("expired"):
        return render_template("expired.html")

    member_id = _member_id_from_form()
    slow = _want_slow()
    if not member_id:
        return render_template(
            "lookup.html",
            error="Member ID is required",
            slow=slow,
        )
    if not DIGIT_RE.match(member_id):
        return render_template(
            "lookup.html",
            error="Invalid member ID",
            slow=slow,
        )
    if member_id not in MEMBERS:
        return render_template("not_found.html")

    session["last_member_id"] = member_id
    return redirect(url_for("profile", member_id=member_id))


@app.get("/member/<member_id>")
def profile(member_id: str):
    member = MEMBERS.get(member_id)
    if member is None:
        return render_template("not_found.html")
    session["last_member_id"] = member_id
    return render_template("profile.html", member_id=member_id, member=member)


@app.get("/sub-account/new")
def sub_account_new():
    return render_template(
        "confirm.html",
        member_id=session.get("last_member_id"),
    )


@app.post("/sub-account/confirm")
def sub_account_confirm():
    # Default mock role is not teller (MOCK_UI.md 3.7).
    return render_template("denied.html")


@app.post("/session/restore")
def session_restore():
    session.pop("expired", None)
    return redirect(url_for("inquiry_get"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=True)
