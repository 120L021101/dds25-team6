local txn_id = ARGV[1]

-- add this transaction to a set
redis.call("SREM", "unrollback_txn_set", txn_id)

return "SUCCESS"
