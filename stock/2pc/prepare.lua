-- prepare.lua
-- stock service

-- item key
local item_key = KEYS[1]
local txn_id = ARGV[1]
local amount = ARGV[2]

local freeze_key = "freeze:" .. item_key
local txn_key = "txn:" .. txn_id

local txn_status = redis.call("GET", txn_key)
if txn_status == "PREPARED" then
    return "PREPARED (before)"
end

if txn_status == "COMMITTED" then
    return "Cannot prepare COMMITTED transaction"
end

if txn_status == "ROLLBACKED" then
    return "Cannot prepare ROLLBACKED transaction"
end
-- local lock_key = "lock:" .. item_key

-- trx key, should be same in a trx

-- Check if freeze_key exists, if not, copy the value from item_key
if redis.call("EXISTS", freeze_key) == 0 then
    local stock_value_structure = redis.call("GET", item_key)
    if stock_value_structure then
        redis.call("SET", freeze_key, stock_value_structure)
    else
        return "Failed, Item not found"
    end
end

local freeze_value_structure = redis.call("GET", freeze_key)
local freeze_value = cmsgpack.unpack(freeze_value_structure)
if tonumber(freeze_value.stock) < tonumber(amount) then
    return "Failed, Stock insufficient!"
end

freeze_value.stock = freeze_value.stock - tonumber(amount)
redis.call("SET", freeze_key, cmsgpack.pack(freeze_value))
redis.call("SET", txn_key, "PREPARED")
redis.call("SET", txn_key .. ":amount", amount)

return "PREPARED"