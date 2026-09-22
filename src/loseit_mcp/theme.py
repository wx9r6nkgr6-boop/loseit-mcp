"""Standalone reference tokens derived from the workout app, never a runtime dependency."""

import json
import re
from pathlib import Path

TOKEN_FILE = Path(__file__).with_name("companion_theme.json")
# Fail-safe neutral dark theme when the packaged reference is missing/invalid.
FALLBACK = (
    dict.fromkeys(("background", "surface", "surfaceElevated"), "#101014")
    | dict.fromkeys(("textPrimary", "textSecondary", "textMuted"), "#EEEEEE")
    | dict.fromkeys(
        ("accentPrimary", "accentSecondary", "violet", "success", "warning", "danger"), "#CCCCCC"
    )
    | {"border": "#555555"}
)


def load_theme(path=TOKEN_FILE):
    try:
        doc = json.loads(Path(path).read_text())
        colors = doc["colors"]
        if set(colors) != set(FALLBACK) or any(
            not isinstance(v, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", v)
            for v in colors.values()
        ):
            raise ValueError("Invalid color tokens")
        if any(
            type(doc[k]) is not int or not 0 <= doc[k] <= 64
            for k in ("radius", "cardPadding", "spacing")
        ):
            raise ValueError("Invalid size tokens")
        return doc | {"fallback": False}
    except (OSError, ValueError, KeyError, TypeError):
        return {
            "colors": FALLBACK.copy(),
            "radius": 8,
            "cardPadding": 16,
            "spacing": 14,
            "fallback": True,
        }


def css(theme):
    c = theme["colors"]
    variables = ";".join(f"--{k}:{v}" for k, v in c.items())
    return f"""<style>
    :root {{{variables};--radius:{theme["radius"]}px;--gap:{theme["spacing"]}px;}}
    html, body {{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;}}
    .stApp {{background:radial-gradient(ellipse at top right,{c["accentPrimary"]}12,transparent 55%),var(--background);color:var(--textPrimary);}}
    [data-testid="stHeader"] {{background:var(--background);}}
    [data-testid="stSidebar"] {{background:var(--surface);border-right:1px solid var(--border);}}
    h1,h2,h3 {{text-transform:uppercase;letter-spacing:.035em;font-weight:900;}}
    h1 {{font-size:clamp(1.55rem,3.4vw,2.35rem)!important;overflow-wrap:anywhere;}}
    h2 {{font-size:clamp(1.1rem,2.3vw,1.55rem)!important;overflow-wrap:anywhere;}}
    h3 {{font-size:clamp(.95rem,1.8vw,1.25rem)!important;overflow-wrap:anywhere;}}
    [data-testid="stCaptionContainer"] {{color:var(--textSecondary);}}
    [data-testid="stMetric"] {{background:var(--surfaceElevated);border:1px solid var(--border);border-top:2px solid var(--accentSecondary);border-radius:var(--radius);padding:{theme["cardPadding"]}px;}}
    [data-testid="stMetricLabel"] p {{white-space:normal;text-transform:uppercase;font-size:.72rem;letter-spacing:.05em;color:var(--textSecondary);}}
    [data-testid="stMetricValue"] {{font-weight:850;color:var(--accentSecondary);font-variant-numeric:tabular-nums;font-size:clamp(1.25rem,3vw,2rem);overflow-wrap:anywhere;}}
    [data-testid="stHorizontalBlock"] {{flex-wrap:wrap;gap:var(--gap);}}
    [data-testid="stColumn"] {{min-width:min(210px,100%);flex:1 1 210px;}}
    [data-testid="stVerticalBlock"] {{gap:clamp(.55rem,1.5vw,var(--gap));}}
    [data-testid="stBaseButton-primary"] {{background:var(--accentPrimary);color:var(--background);font-weight:800;border-radius:var(--radius);border:1px solid var(--accentPrimary);box-shadow:0 0 12px {c["accentPrimary"]}22;}}
    [data-testid="stBaseButton-secondary"] {{color:var(--accentSecondary);border:1px solid var(--border);border-radius:var(--radius);background:var(--surfaceElevated);}}
    button:focus-visible,a:focus-visible {{outline:2px solid var(--accentSecondary)!important;outline-offset:3px;}}
    [role="radiogroup"] label:has(input:checked) {{background:var(--surfaceElevated);border-left:2px solid var(--accentPrimary);border-radius:4px;}}
    [data-testid="stExpander"],[data-testid="stDataFrame"] {{border-radius:var(--radius);border-color:var(--border);max-width:100%;}}
    [data-testid="stDataFrame"] {{overflow:auto;}}
    [data-testid="stTextInput"],[data-testid="stNumberInput"],[data-testid="stSelectbox"] {{width:min(100%,42rem);min-width:min(12rem,100%);}}
    [data-testid="stButton"] button,[data-testid="stFormSubmitButton"] button {{min-height:2.6rem;white-space:normal;overflow-wrap:anywhere;}}
    p,li,td,th,label {{overflow-wrap:anywhere;}}
    canvas,svg {{max-width:100%!important;}}
    [data-testid="stAlert"] {{border-radius:var(--radius);}}
    .block-container {{padding:clamp(1rem,3vw,2rem) clamp(.75rem,3vw,2.5rem) 3rem;max-width:1440px;}}
    @media (max-width:900px) {{
      [data-testid="stColumn"] {{min-width:min(260px,100%);flex-basis:calc(50% - var(--gap));}}
      .block-container {{max-width:100%;}}
    }}
    @media (max-width:560px) {{
      [data-testid="stColumn"] {{min-width:100%;flex-basis:100%;}}
      [data-testid="stHorizontalBlock"] {{gap:.65rem;}}
      [data-testid="stSidebar"] {{min-width:min(88vw,20rem);}}
      [data-testid="stMetric"] {{padding:clamp(.65rem,4vw,1rem);}}
    }}
    </style>"""


def chart_config(theme):
    c = theme["colors"]
    return {
        "config": {
            "background": "transparent",
            "view": {"stroke": None},
            "axis": {
                "labelColor": c["textSecondary"],
                "titleColor": c["textPrimary"],
                "gridColor": c["border"],
                "domainColor": c["border"],
            },
            "legend": {"labelColor": c["textSecondary"], "titleColor": c["textPrimary"]},
            "range": {
                "category": [
                    c[k]
                    for k in ("accentSecondary", "accentPrimary", "success", "warning", "violet")
                ]
            },
        }
    }
