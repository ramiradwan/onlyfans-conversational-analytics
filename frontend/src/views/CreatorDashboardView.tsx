// src/views/CreatorDashboardView.tsx  
import React from 'react';  
import { Grid, Typography, Box, useTheme } from '@mui/material';  
import { KpiCard } from '@components/KpiCard';  
import { useAnalyticsStore } from '@store/analyticsStore';  
import { LineChart } from '@mui/x-charts/LineChart';  
import { BarChart } from '@mui/x-charts/BarChart';  
  
import { ChartPlaceholder, KpiPlaceholder } from '@components/placeholders';  
import { Panel, AsyncContent } from '@components/ui';  
  
export default function CreatorDashboardView() {  
  // --- Data Hooks ---  
  const isLoaded = useAnalyticsStore((s) => s.isLoaded);  
  const topics = useAnalyticsStore((s) => s.topics);  
  const sentimentTrend = useAnalyticsStore((s) => s.sentimentTrend);  
  const responseTimeMetrics = useAnalyticsStore((s) => s.responseTimeMetrics);  
  const unreadCounts = useAnalyticsStore((s) => s.unreadCounts);  
  
  const theme = useTheme();  
  const isLoading = !isLoaded;  
  
  // --- Derived State ---  
  const totalUnread = Object.values(unreadCounts).reduce(  
    (acc, count) => acc + count,  
    0  
  );  
  
  const latestSentiment =  
    sentimentTrend.length > 0  
      ? sentimentTrend[sentimentTrend.length - 1].sentiment_score  
      : 0;  
  
  const kpiData = {  
    avgResponseTime:  
      responseTimeMetrics?.average_handling_time_minutes?.toFixed(1) ?? '…',  
    overallSentiment: `${(latestSentiment * 100).toFixed(0)}%`,  
    totalUnread: totalUnread,  
    silencePercentage:  
      `${responseTimeMetrics?.silence_percentage?.toFixed(0) ?? '…'}%`,  
  };  
  
  const chartHeight = 300;  
  
  return (  
    <Box  
      sx={{  
        bgcolor: theme.vars.palette.background.default,  
        p: { xs: 2, md: 3 },  
      }}  
    >  
      <Typography  
        variant="h4"  
        gutterBottom  
        sx={{ color: theme.vars.palette.text.primary }}  
      >  
        Creator Dashboard  
      </Typography>  
  
      <Grid container spacing={{ xs: 2, md: 3 }}>  
        {/* KPI Row */}  
        {['Avg. Response Time', 'Overall Sentiment', 'Total Unread', 'Silence %'].map(  
          (title, idx) => (  
            <Grid key={idx} size={{ xs: 12, sm: 6, md: 3 }}>  
              {isLoading ? (  
                <KpiPlaceholder />  
              ) : (  
                <KpiCard  
                  title={title}  
                  value={  
                    title === 'Avg. Response Time'  
                      ? `${kpiData.avgResponseTime} min`  
                      : title === 'Overall Sentiment'  
                      ? kpiData.overallSentiment  
                      : title === 'Total Unread'  
                      ? kpiData.totalUnread  
                      : kpiData.silencePercentage  
                  }  
                  isLoading={false}  
                />  
              )}  
            </Grid>  
          )  
        )}  
  
        {/* Sentiment Chart */}  
        <Grid  
          size={{ xs: 12, lg: 8 }}  
          sx={{ display: 'flex', flexDirection: 'column' }}  
        >  
          <Panel sx={{ flexGrow: 1, minHeight: 400 }}>  
            <Typography variant="h6">Sentiment Over Time</Typography>  
            <AsyncContent  
              isLoading={isLoading}  
              data={sentimentTrend}  
              placeholder={<ChartPlaceholder height={chartHeight} />}  
              emptyMessage="No sentiment data available."  
              render={(data) => (  
                <LineChart  
                  aria-label="Sentiment over time chart"  
                  dataset={data}  
                  xAxis={[  
                    {  
                      dataKey: 'date',  
                      scaleType: 'band',  
                      valueFormatter: (dateStr: string) =>  
                        new Date(dateStr).toLocaleDateString(),  
                      hide: true,  
                    },  
                  ]}  
                  yAxis={[{ min: 0, max: 1 }]}  
                  series={[  
                    {  
                      dataKey: 'sentiment_score',  
                      label: 'Sentiment Score',  
                      color: theme.vars.palette.chart.sentiment,  
                      showMark: false,  
                    },  
                  ]}  
                  tooltip={{  
                    trigger: 'item',  
                    valueFormatter: (value: number) =>  
                      `${(value * 100).toFixed(0)}%`,  
                  }}  
                  height={chartHeight}  
                  grid={{ vertical: true, horizontal: true }}  
                />  
              )}  
            />  
          </Panel>  
        </Grid>  
  
        {/* Top Topics Chart */}  
        <Grid  
          size={{ xs: 12, lg: 4 }}  
          sx={{ display: 'flex', flexDirection: 'column' }}  
        >  
          <Panel sx={{ flexGrow: 1, minHeight: 400 }}>  
            <Typography variant="h6">Top Topics by Volume</Typography>  
            <AsyncContent  
              isLoading={isLoading}  
              data={topics}  
              placeholder={<ChartPlaceholder height={chartHeight} />}  
              emptyMessage="No topic data available."  
              render={(data) => (  
                <BarChart  
                  aria-label="Top topics by volume chart"  
                  dataset={data.slice(0, 5)}  
                  yAxis={[{ dataKey: 'topic', scaleType: 'band' }]}  
                  series={[  
                    {  
                      dataKey: 'volume',  
                      label: 'Volume',  
                      color: theme.vars.palette.chart.volume,  
                    },  
                  ]}  
                  layout="horizontal"  
                  height={chartHeight}  
                  grid={{ horizontal: true }}  
                />  
              )}  
            />  
          </Panel>  
        </Grid>  
      </Grid>  
    </Box>  
  );  
}  