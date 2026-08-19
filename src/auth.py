"""パスワード認証・環境変数の初期化。

app.pyの元々の実装をそのまま切り出したもの（挙動は変更していない）。
"""
from __future__ import annotations

import os

import streamlit as st
from dotenv import load_dotenv


def bridge_env_secrets() -> None:
    """.env（ローカル実行）とst.secrets（Streamlit Cloud）の値をos.environに
    橋渡しする。jquants_client.py/edinet_client.py/cache.pyはos.environから
    読む設計のため。st.secretsが未設定（ローカルでsecrets.tomlが無い等）の
    場合はStreamlitSecretNotFoundErrorになるので何もしない。
    """
    load_dotenv()
    try:
        for key in ("JQUANTS_API_KEY", "EDINET_API_KEY", "SUPABASE_URL", "SUPABASE_KEY"):
            if key in st.secrets:
                os.environ[key] = st.secrets[key]
    except Exception:  # noqa: BLE001, S110 -- st.secrets未設定時の想定内フォールバック
        pass


def check_password() -> bool:
    """クラウド公開時、無関係な人に開かれてAPIキー（レート制限）を消費されるのを
    防ぐための簡易パスワード認証。secrets.tomlにapp_passwordが設定されていない
    場合（ローカル実行等）は認証をスキップする。
    """
    try:
        correct = st.secrets["app_password"]
    except (KeyError, FileNotFoundError):
        return True
    if st.session_state.get("authenticated"):
        return True

    st.title("日本株スクリーニング")
    password = st.text_input("パスワード", type="password")
    if password:
        if password == correct:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("パスワードが違います")
    return False
