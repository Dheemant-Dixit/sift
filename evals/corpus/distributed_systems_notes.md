# Distributed Systems Reading Notes

Notes taken while working through a set of papers and textbook chapters on
large scale storage. The prose genre matters for evaluation: unlike forms and
contracts, these paragraphs are self-describing and rarely depend on a heading
several pages above them.

## Consistent hashing

The problem consistent hashing solves is redistribution cost. With a naive
`hash(key) % N` scheme, changing the number of servers from N to N+1 remaps
almost every key, because the modulus changes for all of them. For a cache
sitting in front of a database, that means a near total cache miss storm at
exactly the moment you were trying to add capacity.

Consistent hashing works by mapping both the keys and the servers onto the same
circular keyspace, usually the output range of a hash function treated as a
ring. A key belongs to the first server encountered walking clockwise from the
key's position on the ring. When a server is added, it takes over only the
range of keys lying between itself and its predecessor on the ring. When a
server is removed, its range is absorbed by its successor. In both cases the
number of keys that move is roughly K/N rather than K, where K is the number of
keys and N the number of servers.

Real deployments do not place a server at a single point on the ring, because
one point per server gives badly uneven load. Instead each physical server is
represented by many virtual nodes scattered around the ring, typically a few
hundred. Load then evens out by the law of large numbers, and a heterogeneous
cluster can be expressed by giving more capable machines proportionally more
virtual nodes.

## Bloom filters

A bloom filter is a probabilistic set membership structure. It answers the
question "have I definitely never seen this element?" with certainty, and the
question "have I seen this element?" only with a probability of being wrong.
False positives are possible; false negatives are not.

The structure is a bit array of m bits together with k independent hash
functions. To insert an element you hash it k times, map each result onto the
bit array, and set those k bits. To test membership you hash the element the
same k ways and check whether all k bits are set. If any is clear the element
was definitely never inserted. If all are set the element was probably
inserted, but the bits may have been set by some combination of other elements.

The false positive rate depends on m, k and the number of inserted elements n.
For a fixed m and n the optimal number of hash functions is about (m/n) ln 2,
which gives a false positive probability of roughly 0.6185 raised to the power
m/n. The practical consequence is that around ten bits per element buys a false
positive rate near one percent, which is why bloom filters are so attractive as
a front line filter for expensive lookups such as reads against on disk tables.

Standard bloom filters do not support deletion, because clearing bits would
also clear them for other elements that happen to share them. Counting bloom
filters replace each bit with a small counter to allow removal at the cost of
several times the space.

## Write ahead logging

A write ahead log makes durability separable from the layout of the data. The
rule is that the log record describing a change must reach stable storage
before the page holding the change does. On recovery the system replays the log
from the last checkpoint, redoing committed transactions and undoing those that
were in flight when the process died.

The reason this is faster than writing pages in place is that the log is
append only and therefore sequential, while page writes are scattered. A single
sequential fsync per commit group amortises across many transactions, which is
what group commit exploits.
