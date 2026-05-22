"""Web-based Telegram Session String Generator.

Allows users to log in with their phone number and OTP via the browser,
then generates a Telethon StringSession for use in tgcf's User mode.
"""

import asyncio

import streamlit as st

from tgcf.config import read_config, write_config
from tgcf.web_ui.password import check_password
from tgcf.web_ui.utils import hide_st, switch_theme

CONFIG = read_config()

st.set_page_config(
    page_title="Session Generator",
    page_icon="🔐",
)
hide_st(st)
switch_theme(st, CONFIG)

# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run *coro* in a dedicated thread to avoid conflicting with Streamlit's
    internal event loop."""
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
        return future.result()


async def _connect_and_send_code(api_id: int, api_hash: str, phone: str):
    """Connect, request a login code, disconnect.  Returns (session_data, phone_code_hash)."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    result = await client.send_code_request(phone)
    session_data = client.session.save()
    await client.disconnect()
    return session_data, result.phone_code_hash


async def _sign_in_with_code(
    api_id: int, api_hash: str, session_data: str,
    phone: str, code: str, phone_code_hash: str,
):
    """Reconnect and verify OTP.  Returns (result_str, needs_2fa)."""
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


# ---------------------------------------------------------------------------
# Page UI
# ---------------------------------------------------------------------------

if check_password(st):
    st.write("## 🔐 Session String Generator")
    st.info(
        "Log in with your Telegram account to generate a **Session String**. "
        "This string can then be pasted into the **Telegram Login** page (User mode) — "
        "no need to log in again each time."
    )

    # ------------------------------------------------------------------
    # State machine
    # States: idle | awaiting_code | awaiting_2fa | done | error
    # ------------------------------------------------------------------
    if "sg_state" not in st.session_state:
        st.session_state.sg_state = "idle"

    state = st.session_state.sg_state

    # Reset button
    if state != "idle":
        if st.button("↩ Start over"):
            for key in ["sg_state", "sg_api_id", "sg_api_hash", "sg_session_data",
                        "sg_phone", "sg_hash", "sg_session", "sg_error", "sg_persisted"]:
                st.session_state.pop(key, None)
            st.rerun()

    # ---- STEP 1 : credentials & phone --------------------------------
    if state == "idle":
        with st.form("sg_step1"):
            st.write("### Step 1 – Enter credentials")
            api_id = st.number_input(
                "API ID",
                value=int(CONFIG.login.API_ID) if CONFIG.login.API_ID else 0,
                min_value=0,
            )
            api_hash = st.text_input(
                "API HASH",
                value=CONFIG.login.API_HASH,
                type="password",
            )
            phone = st.text_input(
                "Phone number (international format, e.g. +12025551234)",
                placeholder="+12025551234",
            )
            submitted = st.form_submit_button("Send code")

        if submitted:
            if not api_id or not api_hash or not phone:
                st.error("Please fill in all fields.")
            else:
                with st.spinner("Connecting to Telegram…"):
                    try:
                        session_data, phone_code_hash = _run(
                            _connect_and_send_code(api_id, api_hash, phone)
                        )
                        st.session_state.sg_api_id = api_id
                        st.session_state.sg_api_hash = api_hash
                        st.session_state.sg_session_data = session_data
                        st.session_state.sg_phone = phone
                        st.session_state.sg_hash = phone_code_hash
                        st.session_state.sg_state = "awaiting_code"
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed to send code: {e}")

    # ---- STEP 2 : enter OTP ------------------------------------------
    elif state == "awaiting_code":
        st.success("A verification code has been sent to your Telegram app / SMS.")
        with st.form("sg_step2"):
            st.write("### Step 2 – Enter verification code")
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
                                st.session_state.sg_api_id,
                                st.session_state.sg_api_hash,
                                st.session_state.sg_session_data,
                                st.session_state.sg_phone,
                                code.strip(),
                                st.session_state.sg_hash,
                            )
                        )
                        if needs_2fa:
                            st.session_state.sg_session_data = result
                            st.session_state.sg_state = "awaiting_2fa"
                        else:
                            st.session_state.sg_session = result
                            st.session_state.sg_state = "done"
                        st.rerun()
                    except Exception as e:
                        st.session_state.sg_state = "error"
                        st.session_state.sg_error = str(e)
                        st.rerun()

    # ---- STEP 2b : 2FA password -------------------------------------
    elif state == "awaiting_2fa":
        st.warning("Two-factor authentication is enabled on this account.")
        with st.form("sg_step2fa"):
            st.write("### Step 2b – Enter 2FA password")
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
                                st.session_state.sg_api_id,
                                st.session_state.sg_api_hash,
                                st.session_state.sg_session_data,
                                password,
                            )
                        )
                        st.session_state.sg_session = session_string
                        st.session_state.sg_state = "done"
                        st.rerun()
                    except Exception as e:
                        st.session_state.sg_state = "error"
                        st.session_state.sg_error = str(e)
                        st.rerun()

    # ---- STEP 3 : show session string --------------------------------
    elif state == "done":
        session_string = st.session_state.sg_session
        if not st.session_state.get("sg_persisted", False):
            CONFIG.login.SESSION_STRING = session_string
            CONFIG.login.user_type = 1
            write_config(CONFIG)
            st.session_state.sg_persisted = True

        st.success("✅ Login successful! Your session string is ready and saved to config.")
        st.write("### Your Session String")
        st.code(session_string, language=None)
        st.download_button(
            "📥 Download session string",
            data=session_string,
            file_name="session_string.txt",
            mime="text/plain",
        )

    # ---- Error state --------------------------------------------------
    elif state == "error":
        error = st.session_state.get("sg_error", "Unknown error")
        st.error(f"An error occurred: {error}")
