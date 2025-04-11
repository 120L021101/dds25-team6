# Distributed Data Systems Project

Built on the Two-Phase Commit (2PC) protocol, our system coordinates multiple service instances and Redis cluster to achieve consistency, low-latency responses, and robust fault tolerance. The core transaction logic is implemented using Redis Lua scripts, enabling atomic operations across distributed components.

1. **Order Service** acts as the coordinator of our distributed transaction framework. It follows the 2PC protocol (Prepare \- Commit \- Rollback) to ensure consistency across services. We first perform the operation most prone to failure(which should be stock in this case), and stop early when a failure occurs to reduce the chance of rollbacks. If the initial step succeeds, we proceed with subsequent operations. Unprocessed or incomplete transactions are stored in a Redis set, and a background thread periodically scans and attempts to recover or complete them.

2. **Payment Service** participates in the 2PC protocol to ensure its operations align with the overall transaction consistency. It responds to prepare, commit, or rollback instructions from the Order Service. Because it’s relatively unlikely to visit an account quite simultaneously, in other word, conflicts occur on the payment side not very often, therefore, we directly lock the account during processing the checkout of an order.

3. **Stock Service** uses the TCC (Try \- Confirm \- Cancel) transaction model by temporarily freezing stock to isolate and manage inventory operations with stronger consistency guarantees. To support high concurrency, we avoid strict 2PC here due to its prolonged lock holding, which led to a high failure rate under load. TCC enables more granular control over resource locking and reduces contention during peak usage.
