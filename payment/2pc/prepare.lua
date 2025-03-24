-- prepare.lua

-- id of user, param of "checkout" func call
local user_key = KEYS[1]
local lock_key = "lock:" .. user_key

-- globally unique, id of transaction, could be uuid or timestamp, for now its uuid4
local txn_id = ARGV[1]
local txn_key = "txn:" .. txn_id

if redis.call("SETNX", lock_key, txn_id) == 1 then
    -- redis.call("EXPIRE", lock_key, 120) -- can't be too long, nor too short
    redis.call("SET", txn_key, "PREPARED")
    return "PREPARED" 
else
    return "Failed, Already Locked"
end
