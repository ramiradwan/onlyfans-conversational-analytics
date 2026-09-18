import { Box, QueryResponseBubble } from 'onlyfans-analytics-frontend';

import './card.module.css';

export function AnswerWithResults() {
  return (
    <Box sx={{ maxWidth: 760 }}>
      <QueryResponseBubble
        response={{
          id: 'response-1',
          question: 'Who are my most engaged fans?',
          answer: 'These fans have the strongest recent engagement and response frequency.',
          result: [
            { fan: 'Bailey Hart', engagementScore: 94 },
            { fan: 'Alex River', engagementScore: 88 },
            { fan: 'Casey Lane', engagementScore: 81 },
          ],
          gremlinQuery:
            "g.V().hasLabel('Fan').order().by('engagementScore', decr).limit(3)",
        }}
      />
    </Box>
  );
}

export function ConciseAnswer() {
  return (
    <Box sx={{ maxWidth: 640 }}>
      <QueryResponseBubble
        response={{
          id: 'response-2',
          question: 'What topic is trending?',
          answer: 'Behind-the-scenes content is the fastest-growing conversation topic this week.',
        }}
      />
    </Box>
  );
}
