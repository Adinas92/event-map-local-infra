CREATE DATABASE events;
CREATE DATABASE next_to_you;

\connect events
CREATE EXTENSION IF NOT EXISTS postgis;

\connect next_to_you
CREATE EXTENSION IF NOT EXISTS postgis;
