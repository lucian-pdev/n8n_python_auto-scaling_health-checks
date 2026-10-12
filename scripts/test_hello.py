# Simple connectivity test for python-api
# No external requirements — uses system Python only

name = data.get("name", "World")
numbers = data.get("numbers", [1, 2, 3, 4, 5])

result = {
    "greeting": f"Hello, {name}!",
    "sum": sum(numbers),
    "count": len(numbers),
    "average": sum(numbers) / len(numbers) if numbers else 0,
    "data_received": data
}
