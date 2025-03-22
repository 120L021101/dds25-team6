-- commit.lua
-- stock service

-- item key
local item_key = KEYS[1]
local txn_id = ARGV[1]
local amount = ARGV[2]

local lock_key = "lock:" .. item_key
local txn_key = "txn:" .. txn_id
-- trx key, should be same in a trx

local stock_value_structure = redis.call("GET", item_key)
local stock_value = cmsgpack.unpack(stock_value_structure)
local current_amount = stock_value.stock

local lock_owner = redis.call("GET", lock_key)
local txn_status = redis.call("GET", txn_key)

-- ensure idempotence
if txn_status == "COMMITTED" then
    return "COMMITTED (before)"
end

if tonumber(current_amount) >= tonumber(amount) then
    if lock_owner == txn_key then
    -- upd txn, upd amount, del lock
        stock_value.stock = stock_value.stock - tonumber(amount)
        redis.call("SET", txn_key, "COMMITTED")
        redis.call("SET", item_key, cmsgpack.pack(stock_value))
        redis.call("DEL", lock_key)
        return "COMMITTED"
    else
        return "Fatal Error: Lock not found or not owned by this transaction"
    end
else
    -- Impossible branch
    return "[Impossible Branch] Fatal Error: Stock Value Error "
end
