import asyncio
import threading


_tool_loop = None          # persistent loop for the main (CLI) thread
_worker_thread_local = threading.local()
_tool_loop_lock = threading.Lock()


def _get_worker_loop():
    """Return a persistent event loop for the current worker thread.

    Each worker thread (e.g., delegate_task's ThreadPoolExecutor threads)
    gets its own long-lived loop stored in thread-local storage.  This
    prevents the "Event loop is closed" errors that occurred when
    asyncio.run() was used per-call: asyncio.run() creates a loop, runs
    the coroutine, then *closes* the loop — but cached httpx/AsyncOpenAI
    clients remain bound to that now-dead loop and raise RuntimeError
    during garbage collection or subsequent use.

    By keeping the loop alive for the thread's lifetime, cached clients
    stay valid and their cleanup runs on a live loop.
    """
    loop = getattr(_worker_thread_local, 'loop', None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _worker_thread_local.loop = loop
    return loop

def _get_tool_loop():
    """Return a long-lived event loop for running async tool handlers.

    Using a persistent loop (instead of asyncio.run() which creates and
    *closes* a fresh loop every time) prevents "Event loop is closed"
    errors that occur when cached httpx/AsyncOpenAI clients attempt to
    close their transport on a dead loop during garbage collection.
    """
    global _tool_loop
    with _tool_loop_lock:
        if _tool_loop is None or _tool_loop.is_closed():
            _tool_loop = asyncio.new_event_loop()
        return _tool_loop

def run_async(coro, timeout: float = 300):
    """Run an async coroutine from a sync context.

    If the current thread already has a running event loop (e.g., inside
    the gateway's async stack or Atropos's event loop), we spin up a
    disposable thread so asyncio.run() can create its own loop without
    conflicting.

    For the common CLI path (no running loop), we use a persistent event
    loop so that cached async clients (httpx / AsyncOpenAI) remain bound
    to a live loop and don't trigger "Event loop is closed" on GC.

    When called from a worker thread (parallel tool execution), we use a
    per-thread persistent loop to avoid both contention with the main
    thread's shared loop AND the "Event loop is closed" errors caused by
    asyncio.run()'s create-and-destroy lifecycle.

    This is the single source of truth for sync->async bridging in tool
    handlers. Each handler is self-protecting via this function.

    Args:
        coro: The async coroutine to execute.
        timeout: Optional timeout in seconds. If the coroutine does not
            complete within the given time, a TimeoutError is raised.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Inside an async context (gateway, RL env) — run in a fresh thread
        # with its own event loop we own a reference to, so on timeout we
        # can cancel the task inside that loop (ThreadPoolExecutor.cancel()
        # only works on not-yet-started futures — it's a no-op on a running
        # worker, which previously leaked the thread on every 300 s timeout).
        import concurrent.futures

        worker_loop: asyncio.AbstractEventLoop | None = None
        loop_ready = threading.Event()

        def _run_in_worker():
            nonlocal worker_loop
            worker_loop = asyncio.new_event_loop()
            loop_ready.set()
            try:
                asyncio.set_event_loop(worker_loop)
                _coro = asyncio.wait_for(coro, timeout=timeout)
                return worker_loop.run_until_complete(_coro)
            finally:
                try:
                    # Cancel anything still pending (e.g. task cancelled
                    # externally via call_soon_threadsafe on timeout).
                    pending = asyncio.all_tasks(worker_loop)
                    for t in pending:
                        t.cancel()
                    if pending:
                        worker_loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                except Exception:
                    pass
                worker_loop.close()

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(_run_in_worker)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            # Cancel the coroutine inside its own loop so the worker thread
            # can wind down instead of running forever.
            if loop_ready.wait(timeout=1.0) and worker_loop is not None:
                try:
                    for t in asyncio.all_tasks(worker_loop):
                        worker_loop.call_soon_threadsafe(t.cancel)
                except RuntimeError:
                    # Loop already closed — nothing to cancel.
                    pass
            raise
        finally:
            # wait=False: don't block the caller on a stuck coroutine. We've
            # already requested cancellation above; the worker will exit
            # once the coroutine observes it (usually at the next await).
            pool.shutdown(wait=False)

    # If we're on a worker thread (e.g., parallel tool execution in
    # delegate_task), use a per-thread persistent loop.  This avoids
    # contention with the main thread's shared loop while keeping cached
    # httpx/AsyncOpenAI clients bound to a live loop for the thread's
    # lifetime — preventing "Event loop is closed" on GC cleanup.
    if threading.current_thread() is not threading.main_thread():
        worker_loop = _get_worker_loop()
        if timeout is not None:
            coro = asyncio.wait_for(coro, timeout=timeout)
        return worker_loop.run_until_complete(coro)

    tool_loop = _get_tool_loop()
    if timeout is not None:
        coro = asyncio.wait_for(coro, timeout=timeout)
    return tool_loop.run_until_complete(coro)
