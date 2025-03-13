-- rollback.lua
local user_key = KEYS[1]

local lock_key = user_key .. ":lock"

local txn_id = ARGV[1] 


-- only delete the lock if its still owned by me
if redis.call("GET", lock_key) == txn_id then
   redis.call("DEL", lock_key)
   redis.call("SET", txn_id, "ROLLBACKED")
   return "ROLLBACKED"
end

return "This transaction has no lock, nothing to rollback"