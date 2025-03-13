local content = ARGV[1]

-- remove this transaction from a set
redis.call("SREM", "uncommit_txn_set", content)

return "SUCCESS"
