import React from "react";  
import { Skeleton, Box } from "@mui/material";  
  
/**  
 * Renders a vertical list of skeleton placeholders.  
 *  
 * @param count - How many skeletons to render  
 * @param height - Height of each skeleton  
 * @param mb - Bottom margin between skeletons (default: 2)  
 * @param variant - MUI Skeleton variant (default: "rectangular")  
 */  
export function renderSkeletons(  
  count: number,  
  height: number,  
  mb: number = 2,  
  variant: "rectangular" | "text" | "circular" = "rectangular"  
) {  
  return (  
    <Box>  
      {[...Array(count)].map((_, i) => (  
        <Skeleton  
          key={i}  
          height={height}  
          sx={{ mb, borderRadius: 1 }}  
          variant={variant}  
        />  
      ))}  
    </Box>  
  );  
}  