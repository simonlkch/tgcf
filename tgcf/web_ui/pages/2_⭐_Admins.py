import streamlit as st

from tgcf.config import CONFIG, read_config, write_config
from tgcf.web_ui.password import check_password
from tgcf.web_ui.utils import apply_page_chrome, get_list, get_string, hide_st, switch_theme

CONFIG = read_config()

st.set_page_config(
    page_title="Admins",
    page_icon="⭐",
)
hide_st(st)
switch_theme(st,CONFIG)
if check_password(st):
    apply_page_chrome(
        st,
        CONFIG,
        "Admins",
        "Define who can control tgcf commands and runtime actions.",
        chips=["Access Control", "One Username Per Line"],
    )

    CONFIG.admins = get_list(st.text_area("Admins", value=get_string(CONFIG.admins)))
    st.caption("Add one Telegram username per line.")

    if st.button("Save"):
        write_config(CONFIG)
