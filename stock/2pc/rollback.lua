-- rollback.lua
-- stock service

-- item key
local item_key = KEYS[1]
local txn_id = ARGV[1]

-- local lock_key = "lock:" .. item_key
local freeze_key = "freeze:" .. item_key
local txn_key = "txn:" .. txn_id

local txn_status = redis.call("GET", txn_key)

-- Ensure idempotence
if txn_status == "ROLLBACKED" then
    return "ROLLBACKED (before)"
end

-- Has Commit -- do not rollback
if txn_status == "COMMITTED" then
    return "Cannot rollback COMMITTED transaction"
end

-- Strange state
if txn_status ~= "PREPARED" then
    return "Transaction not in PREPARED state, nothing to rollback"
end

-- Check if freeze_key exists, if not, rollback without any action
-- ?impossible branch?
local reserved_amount = redis.call("GET", txn_key .. ":amount")
if not reserved_amount then
    redis.call("SET", txn_key, "ROLLBACKED")
    return "ROLLBACKED (Inconsistent reservation data)"
end

-- Recover freeze_stock
if redis.call("EXISTS", freeze_key) == 1 then
    local freeze_value_structure = redis.call("GET", freeze_key)
    local freeze_value = cmsgpack.unpack(freeze_value_structure)
    freeze_value.stock = freeze_value.stock + tonumber(reserved_amount)
    redis.call("SET", freeze_key, cmsgpack.pack(freeze_value))
end


redis.call("SET", txn_key, "ROLLBACKED")
redis.call("DEL", txn_key .. ":amount")

return "ROLLBACKED"
-- else