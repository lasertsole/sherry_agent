"""DAO layer — pure data access.\n\nSession purge orchestration lives in ``server/service/session_cleanup_service.py``:\ndeleting one session touches the context engine, the checkpointer, the planning stores\nand the state registers, none of which is this layer's own data.\n"""

__all__: list[str] = []
