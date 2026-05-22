import json
import os
import datetime

import streamlit as st

from tgcf.config import CONFIG_FILE_NAME, Config, read_config, write_config
from tgcf.utils import platform_info
from tgcf.web_ui.password import check_password
from tgcf.web_ui.utils import hide_st, switch_theme

CONFIG = read_config()

st.set_page_config(
    page_title="Advanced",
    page_icon="🔬",
)
hide_st(st)
switch_theme(st,CONFIG)

if check_password(st):

    st.warning("This page is for developers and advanced users.")
    if st.checkbox("I agree"):

        with st.expander("Version & Platform"):
            st.code(platform_info())

        with st.expander("Configuration"):
            with open(CONFIG_FILE_NAME, "r") as file:
                data = json.loads(file.read())
                dumped = json.dumps(data, indent=3)
            st.download_button(
                f"Download config json", data=dumped, file_name=CONFIG_FILE_NAME
            )
            st.json(data)

        with st.expander("Update config.json"):
            st.info("Upload a new config.json to replace the current configuration. The old config will be saved as a backup log.")
            uploaded = st.file_uploader("Upload config.json", type=["json"], key="config_upload")
            if uploaded is not None:
                try:
                    new_raw = uploaded.read().decode("utf-8")
                    new_data = json.loads(new_raw)
                    # Validate it parses as a Config object
                    new_config = Config.parse_raw(new_raw)
                except Exception as e:
                    st.error(f"Invalid config file: {e}")
                else:
                    # Show diff
                    with open(CONFIG_FILE_NAME, "r", encoding="utf8") as f:
                        old_raw = f.read()
                    old_data = json.loads(old_raw)
                    st.write("**Current config (will be replaced):**")
                    st.json(old_data)
                    st.write("**New config (to be applied):**")
                    st.json(new_data)
                    if st.button("Apply new config.json"):
                        # Save backup with timestamp
                        backup_dir = "config_backups"
                        os.makedirs(backup_dir, exist_ok=True)
                        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                        backup_path = os.path.join(backup_dir, f"tgcf.config.backup_{ts}.json")
                        with open(backup_path, "w", encoding="utf8") as bf:
                            bf.write(json.dumps(old_data, indent=3))
                        write_config(new_config)
                        st.success(f"Config updated! Old config saved to `{backup_path}`.")
                        st.rerun()

        with st.expander("Config Backup Logs"):
            backup_dir = "config_backups"
            if os.path.isdir(backup_dir):
                backups = sorted(os.listdir(backup_dir), reverse=True)
                if backups:
                    selected = st.selectbox("Select backup to view", backups)
                    if selected:
                        with open(os.path.join(backup_dir, selected), "r", encoding="utf8") as bf:
                            backup_data = json.loads(bf.read())
                        st.json(backup_data)
                        st.download_button("Download this backup", data=json.dumps(backup_data, indent=3), file_name=selected)
                else:
                    st.info("No config backups yet.")
            else:
                st.info("No config backups yet.")

        with st.expander("Special Options for Live Mode"):
            CONFIG.live.sequential_updates = st.checkbox(
                "Enforce sequential updates", value=CONFIG.live.sequential_updates
            )

            CONFIG.live.delete_on_edit = st.text_input(
                "Delete a message when source edited to",
                value=CONFIG.live.delete_on_edit,
            )
            st.write(
                "When you edit the message in source to something particular, the message will be deleted in both source and destinations."
            )
            if st.checkbox("Customize Bot Messages"):
                st.info(
                    "Note: For userbots, the commands start with `.` instead of `/`, like `.start` and not `/start`"
                )
                CONFIG.bot_messages.start = st.text_area(
                    "Bot's Reply to /start command", value=CONFIG.bot_messages.start
                )
                CONFIG.bot_messages.bot_help = st.text_area(
                    "Bot's Reply to /help command", value=CONFIG.bot_messages.bot_help
                )

            if st.button("Save"):
                write_config(CONFIG)
