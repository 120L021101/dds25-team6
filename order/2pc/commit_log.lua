local log_item = ARGV[1]

-- first, try to delete it from rollback set
redis.call("SREM", "unrollback_txn_set", log_item)

-- then add this transaction to a set
local result = redis.call("SADD", "uncommit_txn_set", log_item)

if result == 1 then
    return "Added Successfully"
elseif result == 0 then
    return "Already Exists"
else
    return "Unexpected Error"
end
