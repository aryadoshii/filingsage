"""Suite-wide test settings.

The API loads the embedding and reranker models in a background thread at
startup (api/main.py, warm_models_on_startup). Any test that runs the app's
lifespan (`with TestClient(app):`) would otherwise load them — on the host,
that means downloading them. Set before any Settings() is built, and env
vars outrank .env, so no developer setting can turn it back on in tests.
"""

import os

os.environ["WARM_MODELS_ON_STARTUP"] = "false"
