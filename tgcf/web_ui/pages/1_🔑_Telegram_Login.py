import streamlit as st
import sys
import os

# 检查并设置Python路径
current_dir = os.path.dirname(os.path.abspath(__file__))
project_dir = os.path.abspath(os.path.join(current_dir, '..', '..', '..'))

if project_dir not in sys.path:
    sys.path.append(project_dir)
    st.write(f'添加项目目录到Python路径: {project_dir}')
else:
    st.write(f'项目目录已在Python路径中: {project_dir}')

# 尝试导入tgcf模块
try:
    from tgcf.config import CONFIG, read_config, write_config
    st.write('成功导入tgcf.config模块')
except ImportError as e:
    st.error(f'导入tgcf.config失败: {e}')
    st.stop()
from tgcf.web_ui.password import check_password
from tgcf.web_ui.utils import hide_st, switch_theme

CONFIG = read_config()

st.set_page_config(
    page_title="Telegram Login",
    page_icon="🔑",
)
hide_st(st)
switch_theme(st,CONFIG)
if check_password(st):
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
        CONFIG.login.SESSION_STRING = st.text_input(
            "Enter session string", value=CONFIG.login.SESSION_STRING, type="password"
        )
        with st.expander("How to get session string ?"):
            st.markdown(
                """

            Link to repl: https://replit.com/@aahnik/tg-login?v=1

            _Click on the above link and enter api id, api hash, and phone no to generate session string._

            **Note from developer:**

            Due some issues logging in with a user account using a phone no is not supported in this web interface.

            I have built a command-line program named tg-login (https://github.com/aahnik/tg-login) that can generate the session string for you.

            You can run tg-login on your computer, or securely in this repl. tg-login is open source, and you can also inspect the bash script running in the repl.

            What is a session string ?
            https://docs.telethon.dev/en/stable/concepts/sessions.html#string-sessions

            """
            )

    if st.button("Save"):
        write_config(CONFIG)
