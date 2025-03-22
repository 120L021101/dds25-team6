-- rollback.lua
-- stock service

-- item key
local item_key = KEYS[1]
local txn_id = ARGV[1]

local lock_key = "lock:" .. item_key
local txn_key = "txn:" .. txn_id

-- Whether the item is locked by this transaction
local lock_owner = redis.call("GET", lock_key)
local txn_status = redis.call("GET", txn_key)

-- Ensure idempotence
if txn_status == "ROLLBACKED" then
    return "ROLLBACKED (before)"
end

-- Only release when the lock is owned by this transaction
if lock_owner == txn_key then
    redis.call("DEL", lock_key)
    redis.call("SET", txn_key, "ROLLBACKED")
    return "ROLLBACKED"
else
    -- No need to rollback
    return "This transaction has no lock on this item, nothing to rollback"
end