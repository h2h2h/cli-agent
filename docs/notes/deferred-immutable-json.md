# Deferred `_json.py` helper

The Runtime currently keeps built-in Tool schemas as ordinary JSON dictionaries.
An earlier `_json.py` helper recursively converted dictionaries to
`MappingProxyType`, lists to tuples, and converted them back to independent JSON
values for serialization.

Its purpose was to prevent accidental nested mutation of shared schema
definitions. That protection is deferred while schemas remain fixed,
Runtime-owned, and internal, because the extra types and conversion code are not
yet justified.

Consider restoring `_json.py` when schemas can enter from external integrations,
cross mutable component boundaries, or produce real mutation/concurrency bugs.
If restored, cover deep immutability and JSON round-trip behavior with contract
tests.
