# Compression and RAG thresholds
ARCHIVE_THRESHOLD = 8_000
MEMORY_THRESHOLD = 10_000
COMPRESS_RATIO = 0.5  # 压缩比例，值越大，旧消息数组（要被压缩的部分）就越大。

# Message bus bounded-queue size (bus/core.py MessageBus). When a queue is
# full the producer awaits free space (backpressure) — messages are delayed,
# never dropped, and memory stays bounded (audit #11).
BUS_QUEUE_MAXSIZE = 1000
