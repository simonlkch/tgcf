import asyncio
import json

import streamlit as st
import streamlit.components.v1 as components

from tgcf.config import CONFIG, SessionEntry, read_config, write_config
from tgcf.web_ui.password import check_password
from tgcf.web_ui.utils import apply_page_chrome, hide_st, switch_theme

CONFIG = read_config()

st.set_page_config(
    page_title="Telegram Login",
    page_icon="🔑",
)
hide_st(st)
switch_theme(st, CONFIG)

# ---------------------------------------------------------------------------
# Async helpers for inline Session String Generator
#
# Each helper is self-contained: it creates a fresh client, does its work,
# disconnects, and returns only serialisable values.  This avoids the
# event-loop binding issue that arises when a TelegramClient created in one
# asyncio loop is reused in a different loop on the next Streamlit rerun.
# ---------------------------------------------------------------------------

def _run(coro):
    """Run *coro* in a dedicated thread so Streamlit's own event loop is
    never touched."""
    import concurrent.futures

    def _thread_target():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_thread_target)
        return future.result()  # re-raises any exception from the thread


async def _connect_and_send_code(api_id: int, api_hash: str, phone: str):
    """Connect, request a login code, disconnect.  Returns (session_data, phone_code_hash)."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    result = await client.send_code_request(phone)
    session_data = client.session.save()  # serialisable – store this, not the client
    await client.disconnect()
    return session_data, result.phone_code_hash


async def _sign_in_with_code(
    api_id: int, api_hash: str, session_data: str,
    phone: str, code: str, phone_code_hash: str,
):
    """Reconnect and verify OTP.  Returns (result_str, needs_2fa).

    * needs_2fa=False  → result_str is the final session string
    * needs_2fa=True   → result_str is intermediate session_data for 2FA
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.errors import SessionPasswordNeededError

    client = TelegramClient(StringSession(session_data), api_id, api_hash)
    await client.connect()
    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        session_string = client.session.save()
        await client.disconnect()
        return session_string, False
    except SessionPasswordNeededError:
        updated_data = client.session.save()
        await client.disconnect()
        return updated_data, True


async def _sign_in_with_2fa(api_id: int, api_hash: str, session_data: str, password: str):
    """Reconnect and complete 2FA.  Returns the final session string."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(session_data), api_id, api_hash)
    await client.connect()
    await client.sign_in(password=password)
    session_string = client.session.save()
    await client.disconnect()
    return session_string


def _render_copy_button(text: str, key: str) -> None:
    """Render a browser-side copy button for a session string."""

    payload = json.dumps(text)
    elem_id = f"copy_status_{key}"
    components.html(
        f"""
        <button
            style=\"padding:0.35rem 0.8rem;border-radius:0.4rem;border:1px solid #aaa;cursor:pointer;\"
            onclick=\"navigator.clipboard.writeText({payload}).then(() => {{document.getElementById('{elem_id}').innerText='Copied';}}).catch(() => {{document.getElementById('{elem_id}').innerText='Copy failed';}});\"
        >Copy Session String</button>
        <span id=\"{elem_id}\" style=\"margin-left:0.6rem;font-size:0.9rem;\"></span>
        """,
        height=42,
    )


# ---------------------------------------------------------------------------
# Page UI
# ---------------------------------------------------------------------------

if check_password(st):
    apply_page_chrome(
        st,
        CONFIG,
        "Telegram Login",
        "Manage bot/user auth and multi-session storage with inline session generation.",
        chips=["Secure Inputs", "Multi Session", "Telethon"],
    )

    CONFIG.login.API_ID = int(
        st.text_input("API ID", value=str(CONFIG.login.API_ID), type="password")
    )
    CONFIG.login.API_HASH = st.text_input(
        "API HASH", value=CONFIG.login.API_HASH, type="password"
    )
    st.write("You can get api id and api hash from https://my.telegram.org.")

    user_type = st.radio(
        "Choose account type", ["Bot", "User"], index=CONFIG.login.user_type
    )

    if user_type == "Bot":
        CONFIG.login.user_type = 0
        CONFIG.login.BOT_TOKEN = st.text_input(
            "Enter bot token", value=CONFIG.login.BOT_TOKEN, type="password"
        )
    else:
        CONFIG.login.user_type = 1

        # ------------------------------------------------------------------
        # Multi-session manager
        # ------------------------------------------------------------------
        st.write("### 👤 User Sessions")

        sessions = CONFIG.login.sessions

        if sessions:
            active_idx = CONFIG.login.active_session
            if active_idx >= len(sessions):
                active_idx = 0
            active_label = sessions[active_idx].name or f"Session {active_idx + 1}"
            st.info(f"**Active session:** #{active_idx + 1} — {active_label}")

            for i, sess in enumerate(sessions):
                col_name, col_status, col_activate, col_delete = st.columns([4, 2, 2, 1])
                with col_name:
                    st.text_input(
                        "Name",
                        value=sess.name,
                        key=f"sess_name_{i}",
                        placeholder=f"Session {i + 1}",
                        label_visibility="collapsed",
                    )
                    masked = (sess.session_string[:10] + "…") if sess.session_string else "(empty)"
                    st.caption(f"`{masked}`")
                with col_status:
                    if CONFIG.login.active_session == i:
                        st.success("✅ Active")
                    else:
                        st.write("")
                with col_activate:
                    if CONFIG.login.active_session != i:
                        if st.button("Set Active", key=f"activate_{i}"):
                            # Flush any pending name edits before saving
                            for j in range(len(CONFIG.login.sessions)):
                                k = f"sess_name_{j}"
                                if k in st.session_state:
                                    CONFIG.login.sessions[j].name = st.session_state[k]
                            CONFIG.login.active_session = i
                            write_config(CONFIG)
                            st.rerun()
                with col_delete:
                    if st.button("🗑", key=f"delete_{i}"):
                        CONFIG.login.sessions.pop(i)
                        if CONFIG.login.active_session >= len(CONFIG.login.sessions):
                            CONFIG.login.active_session = max(0, len(CONFIG.login.sessions) - 1)
                        write_config(CONFIG)
                        st.rerun()
        else:
            st.info("No sessions saved yet. Use the generator below to add one.")

        st.divider()

        # ------------------------------------------------------------------
        # Inline Session String Generator
        # ------------------------------------------------------------------
        if "tl_sg_show" not in st.session_state:
            st.session_state.tl_sg_show = False
        if "tl_sg_state" not in st.session_state:
            st.session_state.tl_sg_state = "idle"

        if not st.session_state.tl_sg_show:
            if st.button("➕ Add Session (Generate Session String)"):
                st.session_state.tl_sg_show = True
                st.session_state.tl_sg_state = "idle"
                st.rerun()
        else:
            st.write("### 🔐 Session String Generator")

            sg_state = st.session_state.tl_sg_state

            if sg_state != "idle":
                if st.button("↩ Start over", key="tl_sg_reset"):
                    for k in ["tl_sg_state", "tl_sg_api_id", "tl_sg_api_hash",
                              "tl_sg_session_data", "tl_sg_phone", "tl_sg_hash",
                              "tl_sg_session", "tl_sg_error", "tl_sg_persisted",
                              "tl_sg_saved_index"]:
                        st.session_state.pop(k, None)
                    st.session_state.tl_sg_show = False
                    st.rerun()

            # Step 1 – phone number
            if sg_state == "idle":
                with st.form("tl_sg_step1"):
                    st.write("**Step 1 – Enter your phone number**")
                    st.caption("API ID and API HASH from the fields above will be used.")
                    phone = st.text_input(
                        "Phone number (international format, e.g. +12025551234)",
                        placeholder="+12025551234",
                    )
                    submitted = st.form_submit_button("Send code")

                if submitted:
                    api_id = CONFIG.login.API_ID
                    api_hash = CONFIG.login.API_HASH
                    if not api_id or not api_hash or not phone:
                        st.error("Please fill in API ID and API HASH above, then enter your phone number.")
                    else:
                        with st.spinner("Connecting to Telegram…"):
                            try:
                                session_data, phone_code_hash = _run(
                                    _connect_and_send_code(api_id, api_hash, phone)
                                )
                                # Store only serialisable values – no client object
                                st.session_state.tl_sg_api_id = api_id
                                st.session_state.tl_sg_api_hash = api_hash
                                st.session_state.tl_sg_session_data = session_data
                                st.session_state.tl_sg_phone = phone
                                st.session_state.tl_sg_hash = phone_code_hash
                                st.session_state.tl_sg_state = "awaiting_code"
                                st.rerun()
                            except Exception as e:
                                st.error(f"Failed to send code: {e}")

            # Step 2 – OTP
            elif sg_state == "awaiting_code":
                st.success("A verification code has been sent to your Telegram app / SMS.")
                with st.form("tl_sg_step2"):
                    st.write("**Step 2 – Enter verification code**")
                    code = st.text_input("Verification code", max_chars=10)
                    submitted = st.form_submit_button("Verify code")

                if submitted:
                    if not code:
                        st.error("Please enter the code.")
                    else:
                        with st.spinner("Verifying…"):
                            try:
                                result, needs_2fa = _run(
                                    _sign_in_with_code(
                                        st.session_state.tl_sg_api_id,
                                        st.session_state.tl_sg_api_hash,
                                        st.session_state.tl_sg_session_data,
                                        st.session_state.tl_sg_phone,
                                        code.strip(),
                                        st.session_state.tl_sg_hash,
                                    )
                                )
                                if needs_2fa:
                                    st.session_state.tl_sg_session_data = result
                                    st.session_state.tl_sg_state = "awaiting_2fa"
                                else:
                                    st.session_state.tl_sg_session = result
                                    st.session_state.tl_sg_state = "done"
                                st.rerun()
                            except Exception as e:
                                st.session_state.tl_sg_state = "error"
                                st.session_state.tl_sg_error = str(e)
                                st.rerun()

            # Step 2b – 2FA
            elif sg_state == "awaiting_2fa":
                st.warning("Two-factor authentication is enabled on this account.")
                with st.form("tl_sg_step2fa"):
                    st.write("**Step 2b – Enter 2FA password**")
                    password = st.text_input("2FA password", type="password")
                    submitted = st.form_submit_button("Submit password")

                if submitted:
                    if not password:
                        st.error("Please enter your 2FA password.")
                    else:
                        with st.spinner("Verifying 2FA…"):
                            try:
                                session_string = _run(
                                    _sign_in_with_2fa(
                                        st.session_state.tl_sg_api_id,
                                        st.session_state.tl_sg_api_hash,
                                        st.session_state.tl_sg_session_data,
                                        password,
                                    )
                                )
                                st.session_state.tl_sg_session = session_string
                                st.session_state.tl_sg_state = "done"
                                st.rerun()
                            except Exception as e:
                                st.session_state.tl_sg_state = "error"
                                st.session_state.tl_sg_error = str(e)
                                st.rerun()

            # Done – save to sessions list
            elif sg_state == "done":
                session_string = st.session_state.tl_sg_session
                if not st.session_state.get("tl_sg_persisted", False):
                    CONFIG.login.SESSION_STRING = session_string
                    CONFIG.login.user_type = 1

                    existing_idx = next(
                        (
                            i
                            for i, sess in enumerate(CONFIG.login.sessions)
                            if sess.session_string == session_string
                        ),
                        None,
                    )
                    if existing_idx is None:
                        default_name = (
                            st.session_state.get("tl_sg_phone", "").strip()
                            or f"Session {len(CONFIG.login.sessions) + 1}"
                        )
                        CONFIG.login.sessions.append(
                            SessionEntry(name=default_name, session_string=session_string)
                        )
                        existing_idx = len(CONFIG.login.sessions) - 1

                    CONFIG.login.active_session = existing_idx
                    write_config(CONFIG)
                    st.session_state.tl_sg_saved_index = existing_idx
                    if 0 <= existing_idx < len(CONFIG.login.sessions):
                        st.session_state[f"sess_name_{existing_idx}"] = (
                            CONFIG.login.sessions[existing_idx].name
                        )
                    st.session_state.tl_sg_persisted = True

                st.success("✅ Login successful! Session saved to config.")
                st.write("### Session String")
                st.code(session_string, language=None)
                _render_copy_button(session_string, "tl_sg_session")
                saved_index = st.session_state.get("tl_sg_saved_index", CONFIG.login.active_session)
                current_name = ""
                if 0 <= saved_index < len(CONFIG.login.sessions):
                    current_name = CONFIG.login.sessions[saved_index].name
                session_name = st.text_input(
                    "Session name (optional)",
                    value=current_name,
                    placeholder="e.g. My Account, Work Account",
                    key="tl_sg_new_name",
                )
                if st.button("💾 Update session name"):
                    name = st.session_state.get("tl_sg_new_name", "")
                    if 0 <= saved_index < len(CONFIG.login.sessions):
                        CONFIG.login.sessions[saved_index].name = name
                        write_config(CONFIG)
                        st.session_state[f"sess_name_{saved_index}"] = name
                    for k in ["tl_sg_state", "tl_sg_api_id", "tl_sg_api_hash",
                              "tl_sg_session_data", "tl_sg_phone", "tl_sg_hash",
                              "tl_sg_session", "tl_sg_error", "tl_sg_new_name",
                              "tl_sg_persisted", "tl_sg_saved_index"]:
                        st.session_state.pop(k, None)
                    st.session_state.tl_sg_show = False
                    st.rerun()

            # Error
            elif sg_state == "error":
                error = st.session_state.get("tl_sg_error", "Unknown error")
                st.error(f"An error occurred: {error}")

    if st.button("Save"):
        # Flush any pending session name edits
        for i in range(len(CONFIG.login.sessions)):
            k = f"sess_name_{i}"
            if k in st.session_state:
                CONFIG.login.sessions[i].name = st.session_state[k]
        write_config(CONFIG)
        st.success("Saved!")
