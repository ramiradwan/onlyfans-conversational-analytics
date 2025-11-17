// src/views/AnalyticsView.tsx  
import {  
  Box,  
  Typography,  
  Grid,  
  useTheme,  
  CircularProgress,  
} from '@mui/material';  
import { BarChart } from '@mui/x-charts/BarChart';  
import { LineChart } from '@mui/x-charts/LineChart';  
import { DataGrid, GridToolbar } from '@mui/x-data-grid';  
  
import React, { useMemo } from 'react';  
  
import type {  
  SentimentTrendPoint,  
  TopicMetricsResponse,  
} from '@/types/backend-wss';  
import {  
  ChartPlaceholder,  
  TablePlaceholder,  
  HorizontalBarsPlaceholder,  
} from '@components/placeholders';  
import { Panel, AsyncContent } from '@components/ui';  
import { useAnalyticsStore } from '@store/analyticsStore';  
  
export default function AnalyticsView() {  
  const theme = useTheme();  
  
  const isLoaded = useAnalyticsStore((state) => state.isLoaded);  
  const topics = useAnalyticsStore((state) => state.topics);  
  const sentimentTrend = useAnalyticsStore((state) => state.sentimentTrend);  
  
  const isLoading = !isLoaded;  
  
  const topicRows = useMemo(  
    () => topics.map((topic, index) => ({ id: index, ...topic })),  
    [topics]  
  );  
  
  const chartHeight = 300;  
  
  return (  
    <Box  
      sx={{  
        flex: 1,  
        display: 'flex',  
        flexDirection: 'column',  
        overflow: 'hidden',  
        bgcolor: theme.vars.palette.background.default,  
        p: { xs: 2, md: 3 },  
      }}  
    >  
      <Typography  
        variant="h4"  
        gutterBottom  
        sx={{ color: theme.vars.palette.text.primary }}  
      >  
        Detailed Analytics  
      </Typography>  
  
      <Grid container spacing={3} sx={{ flex: 1, overflow: 'hidden' }}>  
        {/* Sentiment Pane */}  
        <Grid  
          size={{ xs: 12, md: 6 }}  
          sx={{ display: 'flex', flexDirection: 'column', overflow: 'auto' }}  
        >  
          <Panel sx={{ flex: 1, gap: 3 }}>  
            <Typography variant="h6">  
              Sentiment Over Time (Detailed)  
            </Typography>  
            <AsyncContent<SentimentTrendPoint>  
              isLoading={isLoading}  
              data={sentimentTrend}  
              placeholder={<ChartPlaceholder height={chartHeight} />}  
              emptyMessage="No sentiment data available."  
              render={(data: SentimentTrendPoint[]) => (  
                <LineChart  
                  aria-label="Sentiment over time chart"  
                  // ✅ Cast dataset to chart-safe type to satisfy TS  
                  dataset={  
                    data as unknown as Readonly<  
                      Record<  
                        string,  
                        string | number | Date | null | undefined  
                      >[]  
                    >  
                  }  
                  xAxis={[  
                    {  
                      dataKey: 'date',  
                      scaleType: 'band',  
                      valueFormatter: (dateStr: string) =>  
                        new Date(dateStr).toLocaleDateString(),  
                    },  
                  ]}  
                  yAxis={[{ min: 0, max: 1 }]}  
                  series={[  
                    {  
                      dataKey: 'sentiment_score',  
                      label: 'Sentiment Score',  
                      color: theme.vars.palette.chart.sentiment,  
                    },  
                  ]}  
                  height={chartHeight}  
                  grid={{ vertical: true, horizontal: true }}  
                />  
              )}  
            />  
          </Panel>  
        </Grid>  
  
        {/* Topic Pane */}  
        <Grid  
          size={{ xs: 12, md: 6 }}  
          sx={{ display: 'flex', flexDirection: 'column', overflow: 'auto' }}  
        >  
          <Panel sx={{ flex: 1, gap: 4 }}>  
            <Typography variant="h6">Topic Metrics</Typography>  
            {isLoading ? (  
              <>  
                <Box sx={{ mb: 4 }}>  
                  <TablePlaceholder rows={4} />  
                </Box>  
                <HorizontalBarsPlaceholder bars={5} />  
              </>  
            ) : (  
              <>  
                <Box sx={{ mb: 4 }}>  
                  <DataGrid  
                    rows={topicRows}  
                    columns={[  
                      { field: 'topic', headerName: 'Topic', flex: 1 },  
                      {  
                        field: 'volume',  
                        headerName: 'Volume',  
                        type: 'number',  
                        width: 130,  
                      },  
                      {  
                        field: 'percentage_of_total',  
                        headerName: '% of Total',  
                        type: 'number',  
                        width: 130,  
                        valueFormatter: (value: number) =>  
                          `${(value * 100).toFixed(1)}%`,  
                      },  
                      {  
                        field: 'trend',  
                        headerName: 'Trend (7d)',  
                        type: 'number',  
                        width: 130,  
                        valueFormatter: (value: number) =>  
                          value ? `${(value * 100).toFixed(1)}%` : '–',  
                      },  
                    ]}  
                    density="compact"  
                    autoHeight  
                    slots={{  
                      toolbar: GridToolbar,  
                      noRowsOverlay: () => (  
                        <Typography  
                          color={theme.vars.palette.text.secondary}  
                          align="center"  
                          sx={{ p: 2 }}  
                        >  
                          No topic data available.  
                        </Typography>  
                      ),  
                      loadingOverlay: () => (  
                        <Box  
                          sx={{  
                            display: 'flex',  
                            justifyContent: 'center',  
                            alignItems: 'center',  
                            height: '100%',  
                          }}  
                        >  
                          <CircularProgress size={24} />  
                        </Box>  
                      ),  
                    }}  
                  />  
                </Box>  
                <AsyncContent<TopicMetricsResponse>  
                  isLoading={false}  
                  data={topics}  
                  placeholder={<ChartPlaceholder height={chartHeight} />}  
                  emptyMessage="No topic chart data available."  
                  render={(data: TopicMetricsResponse[]) => (  
                    <BarChart  
                      // ✅ Cast dataset to chart-safe type to satisfy TS  
                      dataset={  
                        data.slice(0, 5) as unknown as Readonly<  
                          Record<  
                            string,  
                            string | number | Date | null | undefined  
                          >[]  
                        >  
                      }  
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
              </>  
            )}  
          </Panel>  
        </Grid>  
      </Grid>  
    </Box>  
  );  
}  