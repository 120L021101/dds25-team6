local txn_id = ARGV[1]

-- add this transaction to a set
local result = redis.call("SADD", "uncommit_txn_set", txn_id)

if result == 1 then
    return "Added Successfully"
elseif result == 0 then
    return "Already Exists"
else
    return "Unexpected Error"
end
