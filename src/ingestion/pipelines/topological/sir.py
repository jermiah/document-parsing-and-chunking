"""Serializable parent/child and sibling pointers with bounded evidence traversal."""


class SIRIndex:
    def __init__(self, nodes, text):
        self.nodes = {node["id"]: node for node in nodes}
        self.text = dict(text)
        for node in nodes:
            node.update(children=[], prev_sibling=None, next_sibling=None)
            self.text.setdefault(node["id"], " > ".join(node.get("lineage", [])))
        for node in nodes:
            parent = self.nodes.get(node["parent_id"])
            if parent is None:
                continue
            siblings = parent["children"]
            if siblings:
                node["prev_sibling"] = siblings[-1]
                self.nodes[siblings[-1]]["next_sibling"] = node["id"]
            siblings.append(node["id"])

    def query(self, ids, limit=24, max_chars=16000):
        """Follow requested nodes, ancestors, nearby siblings and direct children."""
        if not set(ids) <= self.nodes.keys():
            raise ValueError("Unknown SIR node")
        ordered = list(dict.fromkeys(ids))
        for nid in ids:
            node = self.nodes[nid]
            parent = node["parent_id"]
            while parent:
                ordered.append(parent)
                parent = self.nodes[parent]["parent_id"]
            ordered.extend(p for p in (node["prev_sibling"], node["next_sibling"]) if p)
            ordered.extend(node["children"])
        result, used = [], 0
        for nid in dict.fromkeys(ordered):
            if len(result) >= limit or used >= max_chars:
                break
            source = self.text[nid]
            content = source[: min(4000, max_chars - used)]
            used += len(content)
            node = self.nodes[nid]
            result.append(
                {
                    "id": nid,
                    "text": content,
                    "truncated": len(content) < len(source),
                    "parent_id": node["parent_id"],
                    "lineage": node["lineage"],
                    "prev_sibling": node["prev_sibling"],
                    "next_sibling": node["next_sibling"],
                    "children": node["children"],
                    "atomic": node.get("atomic", False),
                }
            )
        return result
