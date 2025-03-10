-- prepare.lua

-- id of user, param of "checkout" func call
local user_key = KEYS[1]
local lock_key = user_key .. ":lock"

-- globally unique, id of transaction, could be uuid or timestamp, for now its uuid4
local txn_key = user_key .. ":txn"
local txn_id = ARGV[1]

if redis.call("SETNX", lock_key, txn_id) == 1 then
    redis.call("EXPIRE", lock_key, 5) -- can't be too long
    redis.call("SET", txn_key, "PREPARED:" .. txn_id)
    return "PREPARED" 
else
    return "NO, Already Locked"
end
