import os
from neo4j import GraphDatabase

URI      = os.getenv("NEO4J_URI", "neo4j://127.0.0.1:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASSWORD", "password123")
AUTH     = (NEO4J_USER, NEO4J_PASS)

_driver = None

def get_driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(URI, auth=AUTH)
    return _driver

def close_driver():
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None