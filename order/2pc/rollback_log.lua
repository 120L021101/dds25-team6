local log_item = ARGV[1]

local result = redis.call("SADD", "unrollback_txn_set", log_item)

if result == 1 then
    return "Added Successfully"
elseif result == 0 then
    return "Already Exists"
else
    return "Unexpected Error"
end
