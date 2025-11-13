import React from "react";  
import { Box, Typography, Paper, Stack, Tooltip } from "@mui/material";  
import { useTheme } from "@mui/material/styles";  
import SentimentSatisfiedIcon from "@mui/icons-material/SentimentSatisfied";  
import SentimentNeutralIcon from "@mui/icons-material/SentimentNeutral";  
import SentimentDissatisfiedIcon from "@mui/icons-material/SentimentDissatisfied";  
import type { AnalyticsUpdate } from "../types/backend-wss";  
import { useChatStore } from "../store/useChatStore";  
  
interface KPIGroupProps {  
  analytics?: Partial<AnalyticsUpdate> | null; // allow null/undefined  
}  
  
export default function KPIGroup({ analytics }: KPIGroupProps) {  
  const theme = useTheme();  
  
  // ✅ Always pull analytics via getter for stable reference  
  const storeAnalytics = useChatStore((s) => s.getAnalytics());  
  const data = analytics ?? storeAnalytics;  
  
  const formatNumber = (num?: number) =>  
    typeof num === "number"  
      ? num.toLocaleString(undefined, { maximumFractionDigits: 0 })  
      : "—";  
  
  const cardSx = {  
    flex: "1 1 160px",  
    maxWidth: 200,  
    p: theme.spacing(1.5),  
    bgcolor: theme.palette.background.paper,  
    borderRadius: theme.shape.borderRadius,  
    display: "flex",  
    flexDirection: "column",  
    alignItems: "flex-start",  
    justifyContent: "center",  
    transition: "transform 0.15s ease",  
    "&:hover": { transform: "translateY(-2px)" },  
  } as const;  
  
  // Build KPI cards dynamically  
  const kpiCards: {  
    label: string;  
    value: string | number;  
    icon?: React.ReactNode;  
    tooltip?: string;  
  }[] = [];  
  
  if (typeof data?.sentiment === "number") {  
    let icon = (  
      <SentimentNeutralIcon  
        fontSize="small"  
        sx={{ color: theme.palette.warning.main }}  
      />  
    );  
    let labelText = "Neutral";  
  
    if (data.sentiment > 0.6) {  
      icon = (  
        <SentimentSatisfiedIcon  
          fontSize="small"  
          sx={{ color: theme.palette.success.main }}  
        />  
      );  
      labelText = "Positive";  
    } else if (data.sentiment < 0.4) {  
      icon = (  
        <SentimentDissatisfiedIcon  
          fontSize="small"  
          sx={{ color: theme.palette.error.main }}  
        />  
      );  
      labelText = "Negative";  
    }  
  
    kpiCards.push({  
      label: "Sentiment",  
      value: `${(data.sentiment * 100).toFixed(0)}%`,  
      icon,  
      tooltip: `${labelText} sentiment`,  
    });  
  }  
  
  if (Array.isArray(data?.topics)) {  
    kpiCards.push({  
      label: "Topics",  
      value: formatNumber(data.topics.length),  
    });  
  }  
  
  if (typeof data?.messageCount === "number") {  
    kpiCards.push({  
      label: "Messages",  
      value: formatNumber(data.messageCount),  
    });  
  }  
  
  if (typeof data?.averageResponseTime === "number") {  
    const minutes = data.averageResponseTime;  
    kpiCards.push({  
      label: "Avg Response",  
      value:  
        minutes >= 60  
          ? `${(minutes / 60).toFixed(1)} hr`  
          : `${minutes} min`,  
    });  
  }  
  
  if (typeof data?.turns === "number") {  
    kpiCards.push({  
      label: "Turns",  
      value: formatNumber(data.turns),  
    });  
  }  
  
  // If no KPIs, render placeholder cards  
  if (!kpiCards.length) {  
    const placeholders = [  
      "Sentiment",  
      "Topics",  
      "Messages",  
      "Avg Response",  
      "Turns",  
    ];  
    return (  
      <Stack direction="row" spacing={2} flexWrap="wrap" sx={{ width: "100%" }}>  
        {placeholders.map((label) => (  
          <Paper  
            key={label}  
            elevation={0}  
            sx={{ ...cardSx, color: theme.palette.text.secondary }}  
          >  
            <Typography variant="subtitle2" sx={{ fontWeight: 500 }}>  
              {label}  
            </Typography>  
            <Typography variant="h6" sx={{ mt: 0.5, fontWeight: 600 }}>  
              —  
            </Typography>  
          </Paper>  
        ))}  
      </Stack>  
    );  
  }  
  
  return (  
    <Stack direction="row" spacing={2} flexWrap="wrap" sx={{ width: "100%" }}>  
      {kpiCards.map((card) => {  
        const CardContent = (  
          <Paper  
            elevation={0}  
            sx={{ ...cardSx, color: theme.palette.text.primary }}  
            aria-label={`${card.label}: ${card.value}`}  
          >  
            <Stack direction="row" spacing={1} alignItems="center">  
              {card.icon}  
              <Typography variant="subtitle2" sx={{ fontWeight: 500 }}>  
                {card.label}  
              </Typography>  
            </Stack>  
            <Typography  
              variant="h6"  
              sx={{ mt: 0.5, fontWeight: 600, wordBreak: "break-word" }}  
            >  
              {card.value}  
            </Typography>  
          </Paper>  
        );  
  
        return card.tooltip ? (  
          <Tooltip key={card.label} title={card.tooltip}>  
            {CardContent}  
          </Tooltip>  
        ) : (  
          <Box key={card.label}>{CardContent}</Box>  
        );  
      })}  
    </Stack>  
  );  
}  