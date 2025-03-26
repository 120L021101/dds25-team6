local log_item = ARGV[1]

-- remove this transaction from a set
redis.call("SREM", "uncommit_txn_set", log_item)

return "SUCCESS"
