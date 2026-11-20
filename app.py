import os

from waitress import serve

from rimbench.web import create_app


if __name__ == "__main__":
    serve(
        create_app(),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "18946")),
        threads=4,
    )
