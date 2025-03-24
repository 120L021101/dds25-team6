-- rollback.lua
local user_key = KEYS[1]

local lock_key = "lock:"..user_key

local txn_id = ARGV[1] 
local txn_key = "txn:" .. txn_id

-- only delete the lock if its still owned by me
if redis.call("GET", lock_key) == txn_id then
   redis.call("DEL", lock_key)
   redis.call("SET", txn_key, "ROLLBACKED")
   return "ROLLBACKED"
end

return "This transaction has no lock, nothing to rollback"