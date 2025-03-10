-- commit.lua

local user_key = KEYS[1]

local lock_key = user_key .. ":lock"
local txn_key = user_key .. ":txn"

local txn_id = ARGV[1] 

if redis.call("GET", txn_key) == "PREPARED:" .. txn_id then
    redis.call("DEL", txn_key)
    redis.call("DEL", lock_key)
    return "COMMITTED"        
else
    return "ERROR: No such transaction, expired(you are too slow) or never exist(check params)"
end
