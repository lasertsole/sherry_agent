import json
import base64
import threading
import streamlit as st
from typing import Any
from pathlib import Path
from urllib.parse import urlencode
from pub_func import sanitize_content
from type.message import MultiModalMessage
from websocket import WebSocket, create_connection
from streamlit.delta_generator import DeltaGenerator
from client.api import post_agent_astream, clear_session
from streamlit.elements.widgets.chat import ChatInputValue
from models.TTS_model import TTS_Request, fetch_TTS_sound
from config import USER_NAME, ASSISTANT_NAME, API_HOST, API_PORT
from streamlit.runtime.uploaded_file_manager import UploadedFile
from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx
from client.utils import storage_add_chat, ChatStorage, clear_session as clear_streamlit_session

st.html("""
<style>
    .stSidebarContent .stChatMessage,
    .stSidebarContent .stChatMessage *{
        display: flex !important;
        align-items: center !important;
        height: fit-content !important;
        margin: 0 !important;
    }
    .stSidebarContent .stChatMessage p{ margin: 0; }

    .stSidebarContent .stChatMessage>div:first-of-type {
        padding: 7px !important;
    }
</style>
""")

# 创建会话ID
session_id = "main"

# 创建streamlit显示容器
st_container: DeltaGenerator = st.container()

# streamlit最大显示对话数
chatStorage = ChatStorage(session_id = session_id, chats_maxlen = 20)

# 创建通知队列
if "notifications" not in st.session_state:
    st.session_state.notifications = []

@st.fragment(run_every="5s")
def render_notifications():
    placeholder = st.empty()
    with placeholder.container():
        for i, item in enumerate(st.session_state.notifications):
            with st.chat_message("info", avatar="🔔"):
                col1, col2 = st.columns([0.8, 0.2])
                col1.write(item)

                if col2.button("X", key=f"close_{i}", type="tertiary"):
                    st.session_state.notifications.pop(i)
                    st.rerun()

#"""以下是创建并保持websocket连接"""
@st.cache_resource
def get_ws() -> WebSocket:
    ws_query_params = {
        "session_id": session_id,
    }
    ws_query_string = urlencode(ws_query_params, doseq=True)
    web_socket: WebSocket = create_connection(f"ws://{API_HOST}:{API_PORT}/sessions/ws?{ws_query_string}")

    def ws_listener(_ws: WebSocket):
        """后台线程：持续监听 WebSocket 消息"""
        try:
            while True:
                message = _ws.recv()
                if message:
                    data = json.loads(message)

                    # 根据消息类型处理
                    event_type = data.get("event", "")
                    content = data.get("content", "")

                    if event_type == "notification":
                        st.session_state.notifications.append(content)

        except Exception as e:
            print(f"WebSocket 监听出错: {e}")

    # 创建后台监听线程
    listener_thread = threading.Thread(target=ws_listener, args=(web_socket,), daemon=True)
    # 将 Streamlit 上下文绑定到线程
    add_script_run_ctx(listener_thread, get_script_run_ctx())
    # 启动线程
    listener_thread.start()

    return web_socket

ws: WebSocket = get_ws()
#"""以上是创建并保持websocket连接"""

#"""以下是侧边栏"""
def sidebar()-> None:
    with st.sidebar:

        # 添加一个按钮
        if st.button("清空会话记录"):
            success, error_msg = clear_session(dict(session_id=session_id))
            if success:
                clear_streamlit_session(session_id=session_id)
                st.rerun()
            else:
                st.error(f"❌ 删除会话记录失败！错误信息：{error_msg}")

        # 添加提醒信息列表
        render_notifications()

#"""以上是侧边"""

# streamlit主程序
def main()-> None:
    chat_list: list[dict[str, Any]] = chatStorage.get_chats()
    if len(chat_list) == 0:
        hello_chat = dict(role="assistant", content=f"{ASSISTANT_NAME}:汉娜さん，来茶间聊天吧！")
        chat_list.append(hello_chat)

    # 创建历史聊天消息UI列表
    with st_container:
        for _chat in chat_list:
            with st.chat_message(_chat["role"], avatar=f"./src/avatar/{_chat['role']}.jpg"):
                st.markdown(_chat["content"])
                if "audio_path_list" in _chat and _chat["audio_path_list"] is not None:
                    for file_path in _chat["audio_path_list"]:
                        if Path(file_path).exists():
                            with open(file_path, "rb") as f:
                                st.audio(data=f.read(), format="audio/ogg")

                if "image_path_list" in _chat and _chat["image_path_list"] is not None:
                    for file_path in _chat["image_path_list"]:
                        if Path(file_path).exists():
                            with open(file_path, "rb") as f:
                                st.image(f.read())

    # 用户输入
    user_input_obj: ChatInputValue = st.chat_input(
        "请输入对话内容",
        accept_file=True,
        file_type=["png", "jpg", "jpeg"],
    )

    if user_input_obj:
        _multi_modal_message: MultiModalMessage = MultiModalMessage(text=user_input_obj.text)
        _files: list[UploadedFile] = user_input_obj.files

        # 添加用户消息框UI
        with st_container:
            with st.chat_message(name = "user", avatar = "./src/avatar/user.jpg"):
                st.markdown(f"{USER_NAME}:{_multi_modal_message.text}")

        # 遍历用户上传图片文件
        image_base64_list: list[str] = []
        for _file in _files:
            # 显示图片
            st.image(_file)

            # 将图片转为bytes
            file_bytes: bytes = _file.getvalue()

            # 将 图片bytes 放入base64列表
            base64_bytes = base64.b64encode(file_bytes)
            base64_string = base64_bytes.decode("utf-8")
            image_base64_list.append(base64_string)

        _multi_modal_message.image_base64_list = image_base64_list if len(image_base64_list) > 0 else None

        storage_add_chat(session_id=session_id, role="user", multi_modal_message=_multi_modal_message)

        # 添加AI消息框UI
        with st_container:
            with st.chat_message(name = "assistant", avatar="./src/avatar/assistant.jpg"):

                request_json = dict(
                    session_id = session_id,
                    multi_modal_message = _multi_modal_message.model_dump(),
                    is_stream = True,
                )

                _content = st.write_stream(post_agent_astream(request_json))

                # 去除开头的ASSISTANT_NAME
                _content = _content[len(f"{ASSISTANT_NAME}:"):]

                # 创建文件列表
                file_list: list[bytes] = []

                with st.spinner("正在生成语音..."):
                    # 生成语音,当生成失败时跳过生成
                    try:
                        # 去除多余字符
                        clear_content = sanitize_content(_content)

                        audio_requires = TTS_Request(text=clear_content, text_lang="zh")
                        response = fetch_TTS_sound(audio_requires)
                        if response is not None:
                            file: bytes = response.content
                            st.audio(data = file, format="audio/ogg")
                            file_list.append(file)
                    except Exception as e:
                        pass

                # 将AI消息持久化
                storage_add_chat(session_id=session_id, role="assistant", multi_modal_message=MultiModalMessage(text=_content, audio_bytes_list=file_list))


# 执行主程序
if __name__ == "__main__":
    sidebar()

    # --- 主界面内容 ---
    main()