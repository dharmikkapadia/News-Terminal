"""streamlit_app.py — entry point for Streamlit Community Cloud.

Community Cloud defaults its "Main file path" to `streamlit_app.py`. The real
app lives in `app.py` (run locally with `streamlit run app.py`); this thin shim
just executes it, so you can deploy without editing that field.

Why runpy instead of `import app`: Streamlit re-runs the main file top-to-bottom
on every interaction. A plain `import app` would execute app.py only on the first
run (Python caches it in sys.modules), so the page would render once and then stop
reacting. runpy.run_path re-executes app.py's source on each rerun, preserving
Streamlit's reactivity. No Streamlit command runs before this, so app.py's
st.set_page_config() is still the first one, as Streamlit requires.
"""

import os
import runpy

_HERE = os.path.dirname(os.path.abspath(__file__))
runpy.run_path(os.path.join(_HERE, "app.py"), run_name="__main__")
