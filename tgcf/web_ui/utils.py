import os
import html
from typing import Dict, List
from tgcf.web_ui.run import package_dir
from tgcf.config import write_config


def get_list(string: str):
    # string where each line is one element
    my_list = []
    for line in string.splitlines():
        clean_line = line.strip()
        if clean_line != "":
            my_list.append(clean_line)
    return my_list


def get_string(my_list: List):
    string = ""
    for item in my_list:
        string += f"{item}\n"
    return string


def dict_to_list(dict: Dict):
    my_list = []
    for key, val in dict.items():
        my_list.append(f"{key}: {val}")
    return my_list


def list_to_dict(my_list: List):
    my_dict = {}
    for item in my_list:
        key, val = item.split(":")
        my_dict[key.strip()] = val.strip()
    return my_dict


def apply_theme(st,CONFIG,hidden_container):
    """Apply theme using browser's local storage"""
    if  st.session_state.theme == '☀️':
        CONFIG.theme = 'light'
    else:
        CONFIG.theme = 'dark'
    write_config(CONFIG)
    st.rerun()


def switch_theme(st,CONFIG):
    """Display the option to change theme (Light/Dark)"""
    with st.sidebar:
        leftpad,content,rightpad = st.columns([0.27,0.46,0.27])
        with content:
            st.radio (
                'Theme:',['☀️','🌒'],
                horizontal=True,
                label_visibility="collapsed",
                index=1 if CONFIG.theme == 'dark' else 0,
                on_change=apply_theme,
                key="theme",
                args=[st,CONFIG,leftpad] # or rightpad
            )
        

def hide_st(st):
    dev = os.getenv("DEV")
    if dev:
        return
    hide_streamlit_style = """
            <style>
            #MainMenu {visibility: hidden;}
            footer {visibility: hidden;}
            </style>
            """
    st.markdown(hide_streamlit_style, unsafe_allow_html=True)


def apply_page_chrome(st, CONFIG, title: str, subtitle: str = "", chips: List[str] = None):
    """Inject a theme-aware visual shell and render a reusable page hero."""

    dark = CONFIG.theme == "dark"
    bg_a = "#08131f" if dark else "#ecfeff"
    bg_b = "#102437" if dark else "#ecfdf5"
    border = "rgba(56,189,248,0.35)" if dark else "rgba(20,184,166,0.32)"
    title_color = "#e2e8f0" if dark else "#134e4a"
    subtitle_color = "#94a3b8" if dark else "#166534"
    chip_bg = "rgba(15, 23, 42, 0.55)" if dark else "rgba(255,255,255,0.82)"
    chip_text = "#cbd5e1" if dark else "#0f172a"
    card_bg = "rgba(15, 23, 42, 0.45)" if dark else "rgba(255,255,255,0.82)"
    text_color = "#e2e8f0" if dark else "#111827"

    st.markdown(
        f"""
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Manrope:wght@400;600;700&display=swap');

            html, body, [class*="css"] {{
                font-family: 'Manrope', sans-serif;
            }}

            h1, h2, h3, .tgcf-page-title {{
                font-family: 'Space Grotesk', sans-serif !important;
                letter-spacing: -0.02em;
            }}

            .tgcf-hero {{
                border: 1px solid {border};
                border-radius: 16px;
                padding: 16px 18px;
                margin: 0 0 12px 0;
                background:
                    radial-gradient(1000px 260px at 10% -20%, rgba(34,211,238,0.18), transparent),
                    radial-gradient(800px 240px at 95% 120%, rgba(16,185,129,0.22), transparent),
                    linear-gradient(135deg, {bg_a}, {bg_b});
            }}

            .tgcf-page-title {{
                margin: 0;
                color: {title_color};
                font-size: 1.36rem;
            }}

            .tgcf-page-subtitle {{
                margin: 6px 0 0 0;
                color: {subtitle_color};
                font-size: 0.95rem;
            }}

            .tgcf-chip {{
                display: inline-block;
                border-radius: 999px;
                padding: 3px 10px;
                margin-right: 6px;
                margin-top: 8px;
                font-size: 0.76rem;
                font-weight: 700;
                color: {chip_text};
                background: {chip_bg};
                border: 1px solid rgba(148, 163, 184, 0.28);
            }}

            .tgcf-surface {{
                background: {card_bg};
                border: 1px solid rgba(148, 163, 184, 0.24);
                border-radius: 12px;
                padding: 10px 12px;
                color: {text_color};
                margin: 8px 0;
            }}
        </style>
        """,
        unsafe_allow_html=True,
    )

    safe_title = html.escape(title)
    safe_subtitle = html.escape(subtitle)
    chip_html = ""
    for chip in chips or []:
        chip_html += f"<span class='tgcf-chip'>{html.escape(str(chip))}</span>"

    st.markdown(
        (
            "<div class='tgcf-hero'>"
            f"<h2 class='tgcf-page-title'>{safe_title}</h2>"
            f"<p class='tgcf-page-subtitle'>{safe_subtitle}</p>"
            f"{chip_html}"
            "</div>"
        ),
        unsafe_allow_html=True,
    )
