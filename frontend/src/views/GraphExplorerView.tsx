import { Box, Stack, Typography } from '@mui/material';  
import React, { useState } from 'react';  
import { QueryInput } from '../components/QueryInput';  
import {  
  QueryResponseBubble,  
  QueryResponseBubbleSkeleton,  
} from '../components/QueryResponseBubble';  
import { UserQueryBubble } from '../components/UserQueryBubble';  
  
// Temporary local type until backend types are generated  
interface QueryResponse {  
  id: string;  
  question: string;  
  answer?: string;  
  gremlinQuery?: string;  
  result?: Record<string, unknown> | unknown[];  
}  
  
export default function GraphExplorerView() {  
  const [history, setHistory] = useState<QueryResponse[]>([]);  
  const [isLoading, setIsLoading] = useState(false);  
  
  const handleSendQuery = (text: string) => {  
    const userQuestion: QueryResponse = {  
      id: `q_${Date.now()}`,  
      question: text,  
    };  
    setHistory((prev) => [userQuestion, ...prev]);  
    setIsLoading(true);  
  
    setTimeout(() => {  
      const aiResponse: QueryResponse = {  
        id: `res_${Date.now()}`,  
        question: text,  
        gremlinQuery:  
          "g.V().hasLabel('Fan').order().by('engagementScore', decr).limit(5)",  
        result: [  
          { fanId: 'fan_123', name: 'AliceFan' },  
          { fanId: 'fan_456', name: 'BobSubscriber' },  
        ],  
        answer: 'Here are your top 5 fans, ranked by their engagement score.',  
      };  
  
      // Replace the question with the answer in history  
      setHistory((prev) => [aiResponse, ...prev.slice(1)]);  
      setIsLoading(false);  
    }, 2000);  
  };  
  
  return (  
    <Box  
      sx={{  
        height: '100%',  
        display: 'flex',  
        flexDirection: 'column',  
        bgcolor: 'background.default', // "content" area  
      }}  
    >  
      {/* Chat History */}  
      <Stack  
        spacing={2}  
        direction="column-reverse"  
        sx={{  
          flex: 1,  
          overflowY: 'auto',  
          p: 2,  
        }}  
      >  
        {isLoading && <QueryResponseBubbleSkeleton />}  
        {history.map((item) =>  
          item.answer ? (  
            <QueryResponseBubble key={item.id} response={item} />  
          ) : (  
            <UserQueryBubble key={item.id} text={item.question} />  
          )  
        )}  
        {history.length === 0 && !isLoading && (  
          <Box  
            sx={{  
              display: 'flex',  
              justifyContent: 'center',  
              alignItems: 'center',  
              height: '100%',  
            }}  
          >  
            <Typography variant="h6" color="text.secondary">  
              Ask a question about your data  
            </Typography>  
          </Box>  
        )}  
      </Stack>  
  
      {/* Input Bar */}  
      <QueryInput onSend={handleSendQuery} disabled={isLoading} />  
    </Box>  
  );  
}  