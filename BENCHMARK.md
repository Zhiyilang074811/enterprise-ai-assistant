# Benchmark Report

## RAG Quality Metrics

| Metric | Value | Target |
|--------|-------|--------|
| Top-K | 5 | >= 5 |
| Chunk Size | 500 | 500 |
| Overlap | 50 | 50 |
| Similarity Threshold | 0.85 | >= 0.80 |

## Performance Targets

| Scenario | Target |
|----------|--------|
| Single query latency | < 3 seconds |
| Concurrent users | >= 40 |
| Knowledge base size | >= 10,000 docs |
| Response streaming | SSE real-time |