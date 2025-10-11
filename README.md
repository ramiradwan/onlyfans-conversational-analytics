# 💬 onlyfans-conversational-analytics  
**Turn your chats into insights. Track conversations, trends, and performance — all in one dashboard.**

---

## 📖 Overview  
`onlyfans-conversational-analytics` helps creators make sense of their conversations by turning raw chat data into easy-to-understand insights.  
Once your conversations are analyzed, you’ll see all your key metrics visualized in an interactive dashboard where you can track engagement, response times, and audience sentiment over time.

---

## 📊 Metrics  
Each topic or theme includes detailed metrics you can filter by date or time range:

| Metric | Description |
|--------|--------------|
| **Volume** | Total number of conversations about a topic. |
| **% of Total** | How much of your overall chat activity that topic represents. |
| **Trend** | Shows whether conversation volume is growing or dropping compared to the previous period. |
| **AHT (min)** | Average handling time — how long your typical chat lasts from start to finish. |
| **% Silence** | Average percentage of silence time (no messages exchanged) across conversations. |
| **Turns** | Average number of times the conversation switches between you and your fan. |
| **Sentiment** | Average mood or tone of your fans, from 0 (negative) to 1 (positive). |

---

## 💡 Why It’s Useful  
- 📊 See what topics drive the most engagement  
- 🕒 Measure your response efficiency  
- 💖 Understand your fans’ overall sentiment  
- 🧠 Identify trends and performance patterns  
- 🔧 Developer-friendly API for custom dashboards and integrations  

---

## 🛠️ Getting Started  
> _Developer setup instructions coming soon._

For now, you can connect your data pipeline and visualize your chat analytics via the provided dashboard once conversations are ingested and processed by Insights.

---
#### **Technical Foundation**
The system design aligns with **Azure Cosmos DB’s Gremlin API** (for graph storage and traversal) and **vector embeddings** (for semantic similarity and NLP-driven inference).  
Data pipelines process text via transformer-based NLP models, extracting entities and relationships to populate a **Labeled Property Graph (LPG)**.

This architecture supports both:
- **Creator analytics dashboards** (via time-series and metric aggregation), and  
- **Psychotherapy research graphs** (via semantic and relational modeling).  
---


## 🧠 About  
Built for **creators who care about connection**

This project also serves as a foundation for graph-based psychotherapy research, modeling interactions and interventions as interconnected nodes and edges to study relational and dynamic patterns of change.


See [Research Releases](https://github.com/ramiradwan/ramiradwan/releases/research) for more information!
